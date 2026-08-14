"""
Deterministic replay: the production execution path an AI agent triggers
when it wants to invoke a previously-discovered capability. No model calls
happen here - every action is a pre-recorded Step played back with
stable-locator resolution (agent.locator_resolver), against fresh input
parameters.

Result contract (schemas.ReplayResult) distinguishes three outcomes that
must never be conflated:
  - SUCCESS           the checkpoint was reached and declared outputs extracted
  - BUSINESS_OUTCOME   a known, expected non-happy-path result occurred
                       (e.g. "no such member") - not a bug, not a crash
  - FAILURE            something the artifact didn't anticipate - stop and
                       surface enough detail (step, expected, observed) to debug
"""
from __future__ import annotations
import re
import time
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

from agent.schemas import (
    Artifact, ArtifactStatus, Step, ActionType, RiskLevel, ElementTarget, Locator,
    ReplayResult, ReplayStatus, FailureDetail,
)
from agent.locator_resolver import resolve, LocatorResolutionError
from agent.guardrails import Guardrails, AllowlistConfig, GuardrailViolation
from agent.evidence import EvidenceRun
from agent.escalation import SessionStore, wait_for_resume

_PARAM_RE = re.compile(r"\{\{(\w+)\}\}")


class ReplayError(Exception):
    """Raised for configuration/precondition problems (bad params, artifact
    not approved) - distinct from a runtime FAILURE result, which is
    returned normally so the caller gets a structured result rather than an
    exception for an ordinary in-band failure."""


def render(template: str | None, params: dict) -> str | None:
    if template is None:
        return None

    def sub(m):
        key = m.group(1)
        if key not in params:
            raise ReplayError(f"template references undeclared param '{{{{{key}}}}}'")
        return str(params[key])

    return _PARAM_RE.sub(sub, template)


def render_target(target: ElementTarget | None, params: dict) -> ElementTarget | None:
    if target is None:
        return None
    def rloc(loc: Locator) -> Locator:
        return loc.model_copy(update={"value": render(loc.value, params)})
    return target.model_copy(update={
        "primary": rloc(target.primary),
        "fallbacks": [rloc(loc) for loc in target.fallbacks],
        "frame_path": [render(x, params) for x in target.frame_path],
    })


class ReplayEngine:
    def __init__(self, allowlist_path: str, evidence_root: str = "evidence",
                 sessions_root: str = "sessions", headless: bool = True, cdp_port: int = 9333):
        self.guardrails = Guardrails(AllowlistConfig.from_yaml(allowlist_path))
        self.evidence_root = evidence_root
        self.session_store = SessionStore(sessions_root)
        self.headless = headless
        self.cdp_port = cdp_port

    def replay(self, artifact: Artifact, params: dict, confirm_risky: bool = False,
               require_approved: bool = False) -> ReplayResult:
        run_id = uuid.uuid4().hex[:12]
        session_id = f"replay-{run_id}"
        evidence = EvidenceRun(self.evidence_root, run_id, "replay")
        t0 = time.time()

        sensitive_params = {p.name for p in artifact.input_params if p.sensitive}
        evidence.log("replay_started", {
            "artifact_id": artifact.artifact_id, "version": artifact.version,
            "params": params,
        }, sensitive_keys=sensitive_params)

        if require_approved and artifact.status != ArtifactStatus.APPROVED:
            evidence.log("blocked_unapproved_artifact", {"status": artifact.status})
            return self._finish(evidence, ReplayResult(
                status=ReplayStatus.FAILURE, artifact_id=artifact.artifact_id,
                artifact_version=artifact.version, run_id=run_id,
                failure=FailureDetail(step_id="preflight", step_index=-1,
                                       expected="artifact.status == approved",
                                       observed=str(artifact.status)),
                evidence_dir=str(evidence.dir),
            ), t0)

        missing = [n for n in artifact.required_params() if n not in params]
        if missing:
            evidence.log("missing_params", {"missing": missing})
            return self._finish(evidence, ReplayResult(
                status=ReplayStatus.FAILURE, artifact_id=artifact.artifact_id,
                artifact_version=artifact.version, run_id=run_id,
                failure=FailureDetail(step_id="preflight", step_index=-1,
                                       expected=f"params include {artifact.required_params()}",
                                       observed=f"missing {missing}"),
                evidence_dir=str(evidence.dir),
            ), t0)

        try:
            self.guardrails.check_domain(artifact.target.entry_url)
        except GuardrailViolation as e:
            evidence.log("guardrail_blocked", {"error": str(e)})
            return self._finish(evidence, ReplayResult(
                status=ReplayStatus.FAILURE, artifact_id=artifact.artifact_id,
                artifact_version=artifact.version, run_id=run_id,
                failure=FailureDetail(step_id="preflight", step_index=-1,
                                       expected="entry_url within allowlist", observed=str(e)),
                evidence_dir=str(evidence.dir),
            ), t0)

        outputs: dict = {}

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless,
                                          args=[f"--remote-debugging-port={self.cdp_port}"])
            context = browser.new_context()
            page = context.new_page()
            self.session_store.create(session_id, f"http://127.0.0.1:{self.cdp_port}",
                                       artifact.target.entry_url, artifact.artifact_id, artifact.goal_template)

            pending_dialog = {"expect": False}

            def on_dialog(dialog):
                if pending_dialog["expect"]:
                    dialog.accept()
                else:
                    evidence.log("unexpected_dialog_auto_dismissed", {"message": dialog.message})
                    dialog.dismiss()
            page.on("dialog", on_dialog)

            result = None
            try:
                from agent.auth import ensure_authenticated
                ensure_authenticated(page, artifact.target.entry_url, evidence=evidence)

                for idx, step in enumerate(artifact.steps):
                    try:
                        self.guardrails.check_action_type(step.action)
                    except GuardrailViolation as e:
                        result = self._fail(artifact, run_id, evidence, page, step, idx,
                                             expected="action within allowlist", observed=str(e))
                        break

                    if step.risk_level == RiskLevel.RISKY:
                        if self.guardrails.requires_confirmation(step.risk_level, confirm_risky):
                            handled = self._escalate_for_confirmation(
                                session_id, evidence, page, step, idx, artifact)
                            if not handled:
                                result = self._fail(artifact, run_id, evidence, page, step, idx,
                                                     expected="human confirmation for risky step",
                                                     observed="no response / escalation timed out")
                                break

                    outcome_result = self._run_step(page, step, params, evidence, pending_dialog,
                                                      outputs, artifact, run_id, idx)
                    if outcome_result is not None:
                        result = outcome_result
                        break
                else:
                    result = self._verify_checkpoint(artifact, page, run_id, evidence, outputs, params)

            except Exception as e:
                evidence.log("hard_failure", {"error": str(e)})
                shot = evidence.screenshot(page, "hard_failure")
                dom = evidence.dom_snapshot(page, "hard_failure")
                result = ReplayResult(
                    status=ReplayStatus.FAILURE, artifact_id=artifact.artifact_id,
                    artifact_version=artifact.version, run_id=run_id,
                    failure=FailureDetail(step_id="unknown", step_index=-1,
                                           expected="no unhandled exception", observed=str(e),
                                           screenshot_path=shot, dom_snapshot_path=dom),
                    evidence_dir=str(evidence.dir),
                )

            self.session_store.complete(session_id)
            browser.close()

        return self._finish(evidence, result, t0)

    # -- per-step execution ------------------------------------------------
    def _run_step(self, page, step: Step, params, evidence, pending_dialog, outputs,
                  artifact, run_id, idx) -> ReplayResult | None:
        """Returns a ReplayResult if the run should stop here (business
        outcome or failure), else None to continue to the next step."""
        pending_dialog["expect"] = step.expect_dialog
        attempts = 0
        max_attempts = 1 + max((c.max_retries for c in step.recoverable_conditions), default=0)

        while attempts < max_attempts:
            attempts += 1
            try:
                if step.action == ActionType.NAVIGATE:
                    url = render(step.url_template, params)
                    self.guardrails.check_domain(url)
                    page.goto(url, timeout=15000)
                else:
                    locator, strategy_used = resolve(page, render_target(step.target, params), timeout_ms=step.timeout_ms)
                    value = render(step.value, params)

                    if step.action == ActionType.CLICK:
                        locator.click(timeout=5000)
                        if step.expect_dialog:
                            page.wait_for_timeout(300)
                    elif step.action == ActionType.FILL:
                        locator.fill(value or "", timeout=5000)
                    elif step.action == ActionType.SELECT:
                        locator.select_option(label=value, timeout=5000)
                    elif step.action == ActionType.ASSERT_TEXT:
                        if (value or "") not in page.content():
                            return self._fail(artifact, run_id, evidence, page, step, idx,
                                               expected=f"page contains '{value}'", observed="not found")
                    elif step.action == ActionType.EXTRACT:
                        text = locator.inner_text(timeout=5000)
                        if step.extract_as:
                            outputs[step.extract_as] = text.strip()

                evidence.log("step_ok", {"step_id": step.step_id, "action": step.action,
                                          "attempt": attempts})

                # after acting, check for a known business outcome or a
                # recoverable interstitial before moving on
                page_text = page.content()
                for ko in step.known_outcomes:
                    if render(ko.matcher.value, params) in page_text:
                        evidence.log("business_outcome_detected", {"code": ko.code})
                        return ReplayResult(
                            status=ReplayStatus.BUSINESS_OUTCOME, artifact_id=artifact.artifact_id,
                            artifact_version=artifact.version, run_id=run_id,
                            business_outcome_code=ko.code,
                            business_outcome_message=ko.message_template,
                            outputs=outputs, evidence_dir=str(evidence.dir),
                        )

                recovered = False
                for rc in step.recoverable_conditions:
                    if render(rc.matcher.value, params) in page_text:
                        evidence.log("recoverable_condition_detected", {
                            "code": rc.code, "strategy": rc.strategy, "attempt": attempts})
                        if attempts >= max_attempts:
                            break
                        page.wait_for_timeout(rc.backoff_ms)
                        if rc.strategy in ("dismiss_and_retry", "wait_and_reload"):
                            page.reload(timeout=10000)
                        recovered = True
                        break

                if recovered:
                    continue  # retry this step
                return None  # step succeeded cleanly, move to next step

            except Exception as e:
                evidence.log("step_error", {"step_id": step.step_id, "attempt": attempts, "error": str(e)})
                if attempts >= max_attempts:
                    return self._fail(artifact, run_id, evidence, page, step, idx,
                                       expected="element resolvable and action succeeds", observed=str(e))
                page.wait_for_timeout(500)
        return None

    def _verify_checkpoint(self, artifact: Artifact, page, run_id, evidence, outputs, params) -> ReplayResult:
        try:
            locator, strategy_used = resolve(page, render_target(artifact.checkpoint.target, params), timeout_ms=5000)
            evidence.log("checkpoint_verified", {"strategy": strategy_used})
            evidence.screenshot(page, "checkpoint")
            return ReplayResult(
                status=ReplayStatus.SUCCESS, artifact_id=artifact.artifact_id,
                artifact_version=artifact.version, run_id=run_id,
                outputs=outputs, evidence_dir=str(evidence.dir),
            )
        except LocatorResolutionError as e:
            shot = evidence.screenshot(page, "checkpoint_failed")
            dom = evidence.dom_snapshot(page, "checkpoint_failed")
            return ReplayResult(
                status=ReplayStatus.FAILURE, artifact_id=artifact.artifact_id,
                artifact_version=artifact.version, run_id=run_id,
                failure=FailureDetail(step_id="checkpoint", step_index=len(artifact.steps),
                                       expected=artifact.checkpoint.description,
                                       observed=f"checkpoint locator unresolved: {e}",
                                       screenshot_path=shot, dom_snapshot_path=dom),
                evidence_dir=str(evidence.dir),
            )

    def _fail(self, artifact, run_id, evidence, page, step, idx, expected, observed) -> ReplayResult:
        shot = evidence.screenshot(page, f"failure_step{idx}")
        dom = evidence.dom_snapshot(page, f"failure_step{idx}")
        evidence.log("hard_failure", {"step_id": step.step_id, "expected": expected, "observed": observed})
        return ReplayResult(
            status=ReplayStatus.FAILURE, artifact_id=artifact.artifact_id,
            artifact_version=artifact.version, run_id=run_id,
            failure=FailureDetail(step_id=step.step_id, step_index=idx, expected=expected,
                                   observed=observed, screenshot_path=shot, dom_snapshot_path=dom),
            evidence_dir=str(evidence.dir),
        )

    def _escalate_for_confirmation(self, session_id, evidence, page, step, idx, artifact) -> bool:
        """Blocks (with a bounded timeout) for a human to confirm a risky
        step via the operator console. Returns True if control was handed
        back (confirmed), False on timeout."""
        shot = evidence.screenshot(page, f"risky_step{idx}_pending_confirmation")
        evidence.log("risky_step_needs_confirmation", {"step_id": step.step_id, "risk_level": step.risk_level})
        self.session_store.request_intervention(
            session_id,
            reason=f"Risky/irreversible step '{step.step_id}' requires human confirmation before executing.",
            step_context={"step_id": step.step_id, "step_index": idx, "artifact_id": artifact.artifact_id},
            screenshot_path=shot,
        )
        try:
            wait_for_resume(self.session_store, session_id, evidence, timeout_seconds=60)
            return True
        except TimeoutError:
            return False

    @staticmethod
    def _finish(evidence, result: ReplayResult, t0: float) -> ReplayResult:
        result.duration_ms = int((time.time() - t0) * 1000)
        evidence.finish({"status": result.status, "business_outcome_code": result.business_outcome_code,
                          "failure": result.failure.model_dump() if result.failure else None})
        return result
