"""
Discovery loop: the model figures out how to accomplish a goal against a
live surface, and the transcript of that success is compiled into a
reusable Artifact (schemas.Artifact).

This is the ONLY place in the system that calls the LLM for a decision.
Everything downstream (replay.py) is deterministic.
"""
from __future__ import annotations
import time
import uuid
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from agent.schemas import (
    Artifact, ArtifactStatus, TargetScope, InputParam, ParamType, Step,
    ActionType, RiskLevel, ElementTarget, Locator, LocatorStrategy,
    OutputField, Checkpoint, KnownOutcome, RecoverableCondition, now_iso,
)
from agent.dom_observer import observe, build_element_target
from agent.guardrails import Guardrails, AllowlistConfig, GuardrailViolation
from agent.evidence import EvidenceRun
from agent.escalation import SessionStore, wait_for_resume


# Domain knowledge attached to artifacts recorded against this target app.
# In a real system this would come from a per-app authoring/review step
# (or a library built up over prior recordings for that vendor product) -
# a single happy-path discovery run cannot itself observe every error state.
# See REPORT.md "Determinism & error handling".
KNOWN_OUTCOME_LIBRARY = {
    "member_lookup": [
        KnownOutcome(
            code="MEMBER_NOT_FOUND",
            message_template="No member found matching the given ID.",
            matcher=Locator(strategy=LocatorStrategy.EXACT_TEXT, value="No member found matching ID",
                             description="search error banner text (substring match applied at replay time)"),
        ),
    ],
    "open_subaccount": [
        KnownOutcome(
            code="PERMISSION_DENIED",
            message_template="Member is in a restricted range; requires supervisor approval.",
            matcher=Locator(strategy=LocatorStrategy.EXACT_TEXT, value="Access Denied",
                             description="restricted-account banner"),
        ),
        KnownOutcome(
            code="VALIDATION_ERROR",
            message_template="Submitted form failed validation.",
            matcher=Locator(strategy=LocatorStrategy.EXACT_TEXT, value="Initial deposit must be",
                             description="inline form validation error"),
        ),
    ],
}
RECOVERABLE_CONDITION_LIBRARY = {
    "open_subaccount": [
        RecoverableCondition(
            code="TRANSIENT_SYSTEM_BUSY",
            matcher=Locator(strategy=LocatorStrategy.EXACT_TEXT, value="System Temporarily Unavailable",
                             description="transient interstitial with a Retry link"),
            strategy="dismiss_and_retry",
            max_retries=2,
            backoff_ms=500,
        ),
    ],
}


class DiscoveryLoop:
    def __init__(self, allowlist_path: str, evidence_root: str = "evidence",
                 artifacts_root: str = "artifacts", sessions_root: str = "sessions",
                 headless: bool = True, cdp_port: int = 9222):
        self.guardrails = Guardrails(AllowlistConfig.from_yaml(allowlist_path))
        self.evidence_root = evidence_root
        self.artifacts_root = Path(artifacts_root)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.session_store = SessionStore(sessions_root)
        self.headless = headless
        self.cdp_port = cdp_port

    def run(self, goal: str, entry_url: str, app_id: str, flow_id: str,
            input_params: dict[str, str], sensitive_params: set[str] | None = None) -> tuple[Artifact | None, str]:
        """flow_id selects which KNOWN_OUTCOME_LIBRARY/RECOVERABLE_CONDITION_LIBRARY
        entries get attached (e.g. 'member_lookup', 'open_subaccount').
        Returns (artifact_or_None, evidence_dir)."""
        sensitive_params = sensitive_params or set()
        run_id = uuid.uuid4().hex[:12]
        session_id = f"discovery-{run_id}"
        evidence = EvidenceRun(self.evidence_root, run_id, "discovery")
        evidence.log("goal", {"goal": goal, "entry_url": entry_url, "input_params": input_params},
                      sensitive_keys=sensitive_params)

        self.guardrails.check_domain(entry_url)

        from agent.llm_client import LLMClient
        llm = LLMClient()
        evidence.log("llm_config", {"provider": llm.provider, "model": llm.model})
        transcript: list[dict] = []  # [{observation, action, result_summary, element_target}]

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=self.headless,
                args=[f"--remote-debugging-port={self.cdp_port}"],
            )
            context = browser.new_context()
            page = context.new_page()
            cdp_endpoint = f"http://127.0.0.1:{self.cdp_port}"
            self.session_store.create(session_id, cdp_endpoint, entry_url, goal=goal)

            pending_dialog = {"expect": False, "action": "accept"}

            def on_dialog(dialog):
                if pending_dialog["expect"]:
                    evidence.log("dialog_handled", {"message": dialog.message, "action": pending_dialog["action"]})
                    if pending_dialog["action"] == "accept":
                        dialog.accept()
                    else:
                        dialog.dismiss()
                else:
                    evidence.log("unexpected_dialog_auto_dismissed", {"message": dialog.message})
                    dialog.dismiss()

            page.on("dialog", on_dialog)

            from agent.auth import ensure_authenticated
            ensure_authenticated(page, entry_url, evidence=evidence)

            page.goto(entry_url, timeout=15000)
            evidence.log("navigated", {"url": entry_url})

            artifact = None
            max_steps = self.guardrails.config.max_steps
            deadline = time.time() + self.guardrails.config.max_run_seconds

            try:
                for step_num in range(1, max_steps + 1):
                    if time.time() > deadline:
                        evidence.log("stopping_condition", {"reason": "timeout"})
                        break

                    obs = observe(page)
                    obs_text = obs.to_prompt()
                    evidence.log("observation", {"step": step_num, "url": obs.url,
                                                  "banner_text": obs.banner_text,
                                                  "n_elements": len(obs.elements)})

                    decision = llm.decide(goal, input_params, obs_text, transcript)
                    evidence.log("decision", {"step": step_num, **decision}, sensitive_keys=sensitive_params)

                    action = decision["action"]

                    if action == "done":
                        evidence.log("goal_reached", {
                            "outputs": decision.get("outputs", {}),
                            "checkpoint_description": decision.get("checkpoint_description", ""),
                        })
                        evidence.screenshot(page, f"step{step_num:02d}_done")
                        artifact = self._compile_artifact(
                            goal=goal, app_id=app_id, entry_url=entry_url, flow_id=flow_id,
                            input_params=input_params, sensitive_params=sensitive_params,
                            transcript=transcript, final_decision=decision, run_id=run_id,
                            model_used=f"{llm.provider}:{llm.model}",
                        )
                        break

                    if action == "stuck":
                        reason = decision.get("stuck_reason", "unspecified")
                        evidence.log("agent_stuck", {"reason": reason})
                        shot = evidence.screenshot(page, f"step{step_num:02d}_stuck")
                        self.session_store.request_intervention(
                            session_id, reason=reason,
                            step_context={"step": step_num, "url": obs.url, "goal": goal},
                            screenshot_path=shot,
                        )
                        try:
                            wait_for_resume(self.session_store, session_id, evidence, timeout_seconds=60)
                        except TimeoutError as e:
                            evidence.log("escalation_timed_out", {"error": str(e)})
                            break
                        continue  # re-observe after human handled it

                    # ---- ordinary action: guardrail check, resolve element, execute ----
                    try:
                        self.guardrails.check_action_type(ActionType(action))
                    except GuardrailViolation as e:
                        evidence.log("guardrail_blocked", {"error": str(e)})
                        break

                    el = None
                    target_dict = None
                    if decision.get("ref"):
                        el = next((e for e in obs.elements if e.ref == decision["ref"]), None)
                        if el is None:
                            evidence.log("invalid_ref", {"ref": decision.get("ref")})
                            transcript.append({"observation": obs_text, "action": decision,
                                                "result_summary": "ERROR: ref not found in observation"})
                            continue
                        target_dict = build_element_target(el)

                    result_summary = self._execute(page, action, el, target_dict, decision,
                                                     pending_dialog, evidence, self.guardrails)
                    transcript.append({
                        "observation": obs_text, "action": decision,
                        "result_summary": result_summary, "element_target": target_dict,
                    })

                else:
                    evidence.log("stopping_condition", {"reason": "max_steps_reached"})

            except Exception as e:
                evidence.log("hard_failure", {"error": str(e)})
                evidence.screenshot(page, "hard_failure")

            self.session_store.complete(session_id)
            browser.close()

        summary = evidence.finish({
            "success": artifact is not None,
            "n_transcript_steps": len(transcript),
            "artifact_id": artifact.artifact_id if artifact else None,
        })

        if artifact:
            out_path = self.artifacts_root / f"{artifact.artifact_id}_v{artifact.version}.json"
            out_path.write_text(artifact.model_dump_json(indent=2))
            evidence.log("artifact_saved", {"path": str(out_path)})

        return artifact, str(evidence.dir)

    # -- execution ------------------------------------------------------
    @staticmethod
    def _execute(page, action, el, target_dict, decision, pending_dialog, evidence, guardrails) -> str:
        value = decision.get("value")
        expect_dialog = bool(decision.get("expect_dialog"))
        pending_dialog["expect"] = expect_dialog
        pending_dialog["action"] = "accept"

        try:
            from agent.locator_resolver import resolve
            from agent.schemas import ElementTarget as ET

            if action == "navigate":
                guardrails.check_domain(value)
                page.goto(value, timeout=10000)
                return f"navigated to {page.url}"

            if action == "assert_text":
                text = page.locator("body").inner_text(timeout=5000)
                ok = (value or "") in text
                return f"assert_text {'PASSED' if ok else 'FAILED'}"

            if not target_dict:
                raise ValueError(f"action '{action}' requires a target ref")

            et = ET(**target_dict)
            locator, strategy_used = resolve(page, et, timeout_ms=8000)

            if action == "click":
                locator.click(timeout=5000)
                if expect_dialog:
                    page.wait_for_timeout(300)  # let the dialog handler fire
                return f"clicked via {strategy_used}"
            if action == "fill":
                locator.fill(value or "", timeout=5000)
                return f"filled via {strategy_used}"
            if action == "select":
                locator.select_option(label=value, timeout=5000)
                return f"selected '{value}' via {strategy_used}"
            if action == "extract":
                text = locator.inner_text(timeout=5000)
                return f"extracted '{text}' via {strategy_used}"

            return f"no-op for action={action}"
        except Exception as e:
            evidence.log("action_error", {"action": action, "error": str(e)})
            return f"ERROR: {e}"

    # -- transcript -> artifact compilation ------------------------------
    def _compile_artifact(self, goal, app_id, entry_url, flow_id, input_params,
                           sensitive_params, transcript, final_decision, run_id, model_used) -> Artifact:
        steps: list[Step] = []
        outputs: list[OutputField] = []
        checkpoint = None

        # opening navigate step
        steps.append(Step(
            step_id="s0_navigate",
            action=ActionType.NAVIGATE,
            url_template=self._templatize(entry_url, input_params),
            risk_level=RiskLevel.SAFE,
            notes="entry point",
        ))

        for i, turn in enumerate(transcript, start=1):
            a = turn["action"]
            act_type = a["action"]
            if act_type not in ("click", "fill", "select", "extract", "assert_text"):
                continue

            step_id = f"s{i}_{act_type}"
            target = None
            if turn.get("element_target"):
                target = ElementTarget(**turn["element_target"])

            value = a.get("value")
            templated_value = self._templatize(value, input_params) if value else value

            risk = RiskLevel.SAFE if act_type in ("extract", "assert_text") else RiskLevel.REVERSIBLE
            # heuristic: a click whose action is expected to submit a
            # confirm()-guarded form is the state-changing, hard-to-reverse
            # step in this flow - flag it RISKY explicitly.
            if act_type == "click" and a.get("expect_dialog"):
                risk = RiskLevel.RISKY

            if act_type == "assert_text":
                if "PASSED" not in turn.get("result_summary", ""):
                    continue
                checkpoint = Checkpoint(
                    description=final_decision.get("checkpoint_description", f"page contains '{value}'"),
                    target=ElementTarget(primary=Locator(
                        strategy=LocatorStrategy.EXACT_TEXT,
                        value=self._templatize(value or "", input_params),
                        description="checkpoint text",
                    )),
                )
                continue

            step = Step(
                step_id=step_id,
                action=ActionType(act_type),
                target=target,
                value=templated_value,
                extract_as=a.get("extract_field_name") if act_type == "extract" else None,
                expect_dialog=bool(a.get("expect_dialog")),
                risk_level=risk,
                notes=a.get("reasoning", ""),
            )
            steps.append(step)

            if act_type == "extract" and a.get("extract_field_name"):
                outputs.append(OutputField(
                    name=a["extract_field_name"], type=ParamType.STRING,
                    description=f"extracted at {step_id}", source_step_id=step_id,
                ))

        if checkpoint is None:
            raise ValueError(
                "Discovery reached done() without a successful assert_text checkpoint; "
                "refusing to emit a replay artifact that cannot verify success."
            )

        declared = set((final_decision.get("outputs") or {}).keys())
        extracted = {o.name for o in outputs}
        unextracted = declared - extracted
        if unextracted:
            raise ValueError(
                f"Model reported outputs that were not explicitly extracted: {sorted(unextracted)}"
            )

        input_param_models = [
            InputParam(name=k, type=ParamType.STRING, required=True,
                       sensitive=(k in sensitive_params), description=f"input parameter '{k}'")
            for k in input_params
        ]

        # attach domain-knowledge error taxonomy for this flow (see module docstring)
        known = KNOWN_OUTCOME_LIBRARY.get(flow_id, [])
        recoverable = RECOVERABLE_CONDITION_LIBRARY.get(flow_id, [])
        if known or recoverable:
            # attach to the last state-changing/navigating step, since that's
            # where these conditions are actually observed in this app
            target_step = next((s for s in reversed(steps) if s.action in (ActionType.CLICK, ActionType.NAVIGATE)), steps[-1])
            target_step.known_outcomes = known
            target_step.recoverable_conditions = recoverable

        artifact_id = f"{app_id}.{flow_id}"
        return Artifact(
            artifact_id=artifact_id,
            name=flow_id.replace("_", " ").title(),
            version=1,
            status=ArtifactStatus.DRAFT,
            description=f"Discovered capability for goal: {goal}",
            goal_template=goal,
            target=TargetScope(app_id=app_id, entry_url=self._templatize(entry_url, input_params),
                                allowed_domains=self.guardrails.config.allowed_domains),
            input_params=input_param_models,
            steps=steps,
            outputs=outputs,
            checkpoint=checkpoint,
            created_from_run_id=run_id,
            model_used=model_used,
        )

    @staticmethod
    def _templatize(value: str, input_params: dict[str, str]) -> str:
        result = value
        for k, v in sorted(input_params.items(), key=lambda item: len(str(item[1])), reverse=True):
            if v:
                result = result.replace(str(v), "{{" + k + "}}")
        return result
