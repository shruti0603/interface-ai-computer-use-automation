"""
Unit tests that exercise the artifact schema, guardrails, and templating
logic without needing a real browser - fast, deterministic, run in CI.
Browser-dependent behavior (locator resolution against a live page, the
discovery loop, full replay) is exercised by the /evidence/ runs instead,
since it needs Playwright's Chromium binary.
"""
import pytest
from pydantic import ValidationError

from agent.schemas import (
    Artifact, ArtifactStatus, TargetScope, InputParam, ParamType, Step,
    ActionType, RiskLevel, ElementTarget, Locator, LocatorStrategy,
    OutputField, Checkpoint, KnownOutcome,
)
from agent.guardrails import (
    AllowlistConfig, Guardrails, GuardrailViolation, redact_text, redact_dict,
)
from agent.replay import render, ReplayError
from agent.dom_observer import ObservedElement, PageObservation, build_element_target


def make_artifact(**overrides) -> Artifact:
    defaults = dict(
        artifact_id="meridian.member_lookup",
        name="Member Lookup",
        description="test",
        goal_template="look up a member",
        target=TargetScope(app_id="meridian", entry_url="http://127.0.0.1:5055/search",
                            allowed_domains=["127.0.0.1:5055"]),
        input_params=[InputParam(name="member_id", type=ParamType.STRING, required=True)],
        steps=[
            Step(step_id="s0", action=ActionType.NAVIGATE, url_template="http://127.0.0.1:5055/search"),
            Step(step_id="s1", action=ActionType.FILL, value="{{member_id}}",
                 target=ElementTarget(primary=Locator(strategy=LocatorStrategy.FORM_FIELD_NAME, value="member_id"))),
        ],
        outputs=[OutputField(name="savings_balance", type=ParamType.STRING, source_step_id="s2")],
        checkpoint=Checkpoint(description="member page shown",
                               target=ElementTarget(primary=Locator(strategy=LocatorStrategy.EXACT_TEXT, value="Member Record"))),
    )
    defaults.update(overrides)
    return Artifact(**defaults)


# ---------------------------------------------------------------------------
# Artifact schema
# ---------------------------------------------------------------------------

def test_artifact_round_trips_through_json():
    a = make_artifact()
    dumped = a.model_dump_json()
    restored = Artifact.model_validate_json(dumped)
    assert restored.artifact_id == a.artifact_id
    assert restored.steps[1].value == "{{member_id}}"


def test_required_params():
    a = make_artifact(input_params=[
        InputParam(name="member_id", type=ParamType.STRING, required=True),
        InputParam(name="note", type=ParamType.STRING, required=False),
    ])
    assert a.required_params() == ["member_id"]


def test_artifact_rejects_bad_enum():
    with pytest.raises(ValidationError):
        Step(step_id="s1", action="not_a_real_action")


# ---------------------------------------------------------------------------
# Guardrails: allowlist
# ---------------------------------------------------------------------------

def make_guardrails(**overrides):
    cfg = dict(
        allowed_domains=["127.0.0.1:5055"],
        allowed_action_types=["navigate", "click", "fill", "select", "wait_for", "extract", "assert_text"],
        risky_action_policy="require_confirmation",
        max_steps=40, max_run_seconds=180,
    )
    cfg.update(overrides)
    return Guardrails(AllowlistConfig(**cfg))


def test_allowlist_permits_configured_domain():
    g = make_guardrails()
    g.check_domain("http://127.0.0.1:5055/search")  # should not raise


def test_allowlist_blocks_other_domain():
    g = make_guardrails()
    with pytest.raises(GuardrailViolation):
        g.check_domain("https://evil.example.com/phish")


def test_allowlist_blocks_disallowed_action_type():
    g = make_guardrails(allowed_action_types=["navigate", "extract"])
    with pytest.raises(GuardrailViolation):
        g.check_action_type(ActionType.CLICK)


# ---------------------------------------------------------------------------
# Guardrails: risk policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("policy,confirmed,expected", [
    ("block", True, True),           # block policy never lets risky through, even if "confirmed"
    ("block", False, True),
    ("require_confirmation", False, True),
    ("require_confirmation", True, False),
    ("allow_with_flag", False, False),
])
def test_requires_confirmation_matrix(policy, confirmed, expected):
    g = make_guardrails(risky_action_policy=policy)
    assert g.requires_confirmation(RiskLevel.RISKY, confirmed) == expected


def test_safe_actions_never_require_confirmation():
    g = make_guardrails()
    assert g.requires_confirmation(RiskLevel.SAFE, confirmed=False) is False
    assert g.requires_confirmation(RiskLevel.REVERSIBLE, confirmed=False) is False


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def test_redact_text_masks_ssn_like_and_secrets():
    assert redact_text("ssn 123-45-6789 on file") == "ssn [REDACTED] on file"
    assert "[REDACTED]" in redact_text("api_key: sk-abc123XYZ")
    assert "[REDACTED]" in redact_text("password=hunter2")


def test_redact_dict_masks_named_sensitive_fields():
    out = redact_dict({"member_id": "12345", "note": "fine"}, sensitive_keys={"member_id"})
    assert out["member_id"] == "[REDACTED]"
    assert out["note"] == "fine"


# ---------------------------------------------------------------------------
# Templating (replay.render)
# ---------------------------------------------------------------------------

def test_render_substitutes_param():
    assert render("member id is {{member_id}}", {"member_id": "12345"}) == "member id is 12345"


def test_render_passthrough_none():
    assert render(None, {}) is None


def test_render_raises_on_undeclared_param():
    with pytest.raises(ReplayError):
        render("{{missing}}", {})


# ---------------------------------------------------------------------------
# dom_observer locator generation
# ---------------------------------------------------------------------------

def test_build_element_target_prefers_test_id_over_everything():
    el = ObservedElement(ref="e1", tag="input", type="text", role="textbox",
                          name_attr="member_id", id_attr="mid", test_id="member-id-input",
                          aria_label=None, placeholder=None, href=None, text="",
                          label_text="Member ID", options=None)
    t = build_element_target(el)
    assert t["primary"]["strategy"] == "test_id"
    assert t["primary"]["value"] == "member-id-input"
    strategies = [t["primary"]["strategy"]] + [f["strategy"] for f in t["fallbacks"]]
    assert "form_field_name" in strategies
    assert "label_text" in strategies


def test_build_element_target_falls_back_to_label_text_when_no_name_or_testid():
    el = ObservedElement(ref="e2", tag="input", type="text", role="textbox",
                          name_attr=None, id_attr=None, test_id=None,
                          aria_label=None, placeholder=None, href=None, text="",
                          label_text="Initial Deposit", options=None)
    t = build_element_target(el)
    assert t["primary"]["strategy"] == "label_text"
    assert t["primary"]["value"] == "Initial Deposit"


def test_build_element_target_never_empty():
    el = ObservedElement(ref="e3", tag="button", type=None, role="button",
                          name_attr=None, id_attr=None, test_id=None,
                          aria_label=None, placeholder=None, href=None, text="",
                          label_text=None, options=None)
    t = build_element_target(el)
    assert t["primary"] is not None

# ---------------------------------------------------------------------------
# Regression coverage for reusable artifacts
# ---------------------------------------------------------------------------

def test_templatize_replaces_param_inside_url():
    from agent.agent_loop import DiscoveryLoop
    value = "http://127.0.0.1:5055/member/12345/new-subaccount"
    assert DiscoveryLoop._templatize(value, {"member_id": "12345"}) == \
        "http://127.0.0.1:5055/member/{{member_id}}/new-subaccount"


def test_render_target_substitutes_locator_param():
    from agent.replay import render_target
    from agent.schemas import ElementTarget, Locator, LocatorStrategy
    target = ElementTarget(primary=Locator(
        strategy=LocatorStrategy.EXACT_TEXT,
        value="Member Record: {{member_id}}",
    ))
    rendered = render_target(target, {"member_id": "00000"})
    assert rendered.primary.value == "Member Record: 00000"

def test_compile_artifact_uses_assert_text_as_checkpoint_not_replay_step(tmp_path):
    from agent.agent_loop import DiscoveryLoop
    cfg = tmp_path / "allowlist.yaml"
    cfg.write_text("""
allowed_domains: [\"127.0.0.1:5055\"]
allowed_action_types: [navigate, click, fill, select, wait_for, extract, assert_text]
risky_action_policy: require_confirmation
max_steps: 40
max_run_seconds: 180
""")
    loop = DiscoveryLoop(str(cfg), evidence_root=str(tmp_path / "evidence"),
                         artifacts_root=str(tmp_path / "artifacts"),
                         sessions_root=str(tmp_path / "sessions"))
    transcript = [{
        "action": {"action": "assert_text", "value": "Member Record: 12345", "reasoning": "proof"},
        "element_target": None,
        "observation": "",
        "result_summary": "assert_text PASSED",
    }]
    artifact = loop._compile_artifact(
        goal="Look up member 12345", app_id="meridian",
        entry_url="http://127.0.0.1:5055/member/12345", flow_id="member_lookup",
        input_params={"member_id": "12345"}, sensitive_params=set(),
        transcript=transcript,
        final_decision={"outputs": {}, "checkpoint_description": "member page visible"},
        run_id="run1", model_used="test-model",
    )
    assert artifact.steps[0].url_template == "http://127.0.0.1:5055/member/{{member_id}}"
    assert all(step.action != ActionType.ASSERT_TEXT for step in artifact.steps)
    assert artifact.checkpoint.target.primary.value == "Member Record: {{member_id}}"


def test_build_element_target_for_readable_text_uses_exact_text():
    el = ObservedElement(
        ref="e9", tag="td", type=None, role="text",
        name_attr=None, id_attr=None, test_id=None, aria_label=None,
        placeholder=None, href=None, text="$4820.55", label_text="Savings",
        interactive=False, options=None,
    )
    target = build_element_target(el)
    assert target["primary"]["strategy"] == "exact_text"
    assert any(c["strategy"] == "exact_text" and c["value"] == "$4820.55"
               for c in [target["primary"], *target["fallbacks"]])


def test_page_observation_prompt_includes_visible_read_only_text():
    obs = PageObservation(
        url="http://example.test/member/12345",
        title="Member 12345",
        elements=[],
        banner_text="",
        visible_text="Savings $4820.55\nChecking $1210.10",
    )
    prompt = obs.to_prompt()
    assert "VISIBLE PAGE TEXT:" in prompt
    assert "Savings $4820.55" in prompt
    assert "PAGE ELEMENTS (interactive + readable):" in prompt
