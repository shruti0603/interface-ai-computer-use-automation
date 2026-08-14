"""
Exercises ReplayEngine._run_step control flow using a fake Playwright Page
so the business-outcome / recoverable-condition / hard-failure branching is
tested even though this sandbox can't download a real Chromium binary.
Real end-to-end behavior against the actual target app is what the
/evidence/ discovery+replay runs (produced by `python cli.py ...` with a
real browser) demonstrate; these tests pin down the *logic*.
"""
from unittest.mock import MagicMock, patch
import pytest

from agent.schemas import (
    Step, ActionType, RiskLevel, ElementTarget, Locator, LocatorStrategy,
    KnownOutcome, RecoverableCondition, ReplayStatus,
)
from agent.evidence import EvidenceRun
from agent.replay import ReplayEngine


class FakeLocator:
    def __init__(self, text=""):
        self._text = text
        self.click = MagicMock()
        self.fill = MagicMock()
        self.select_option = MagicMock()

    def inner_text(self, timeout=5000):
        return self._text


def make_engine(tmp_path, allowed_domains=None, policy="require_confirmation"):
    cfg_path = tmp_path / "allowlist.yaml"
    cfg_path.write_text(f"""
allowed_domains: {allowed_domains or ["127.0.0.1:5055"]}
allowed_action_types: [navigate, click, fill, select, wait_for, extract, assert_text]
risky_action_policy: {policy}
max_steps: 40
max_run_seconds: 180
""")
    engine = ReplayEngine(str(cfg_path), evidence_root=str(tmp_path / "evidence"),
                           sessions_root=str(tmp_path / "sessions"))
    return engine


def make_evidence(tmp_path):
    return EvidenceRun(str(tmp_path / "evidence"), "test-run", "replay")


@pytest.fixture
def click_step():
    return Step(
        step_id="s1", action=ActionType.CLICK, risk_level=RiskLevel.SAFE,
        target=ElementTarget(primary=Locator(strategy=LocatorStrategy.EXACT_TEXT, value="Look Up Member")),
        known_outcomes=[
            KnownOutcome(code="MEMBER_NOT_FOUND", message_template="no such member",
                         matcher=Locator(strategy=LocatorStrategy.EXACT_TEXT, value="No member found matching ID")),
        ],
        recoverable_conditions=[
            RecoverableCondition(code="TRANSIENT", strategy="dismiss_and_retry", max_retries=2, backoff_ms=1,
                                  matcher=Locator(strategy=LocatorStrategy.EXACT_TEXT, value="System Temporarily Unavailable")),
        ],
    )


def test_run_step_detects_known_business_outcome(tmp_path, click_step):
    engine = make_engine(tmp_path)
    evidence = make_evidence(tmp_path)
    page = MagicMock()
    page.content.return_value = "<html>No member found matching ID '99999'.</html>"

    with patch("agent.replay.resolve", return_value=(FakeLocator(), "exact_text")):
        result = engine._run_step(page, click_step, {}, evidence, {"expect": False},
                                   outputs={}, artifact=MagicMock(artifact_id="a", version=1),
                                   run_id="r1", idx=0)

    assert result is not None
    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.business_outcome_code == "MEMBER_NOT_FOUND"


def test_run_step_recovers_from_transient_condition_then_succeeds(tmp_path, click_step):
    engine = make_engine(tmp_path)
    evidence = make_evidence(tmp_path)
    page = MagicMock()
    # first content() check: transient banner; after "reload", clean page
    contents = iter([
        "<html>System Temporarily Unavailable</html>",
        "<html>all good now</html>",
    ])
    page.content.side_effect = lambda: next(contents)

    with patch("agent.replay.resolve", return_value=(FakeLocator(), "exact_text")):
        result = engine._run_step(page, click_step, {}, evidence, {"expect": False},
                                   outputs={}, artifact=MagicMock(artifact_id="a", version=1),
                                   run_id="r1", idx=0)

    assert result is None  # step succeeded cleanly after one recovery -> proceed to next step
    assert page.reload.called


def test_run_step_hard_failure_when_locator_never_resolves(tmp_path, click_step):
    from agent.locator_resolver import LocatorResolutionError
    engine = make_engine(tmp_path)
    evidence = make_evidence(tmp_path)
    page = MagicMock()
    page.content.return_value = "<html>unrelated page</html>"

    with patch("agent.replay.resolve", side_effect=LocatorResolutionError(click_step.target, ["exact_text:x"])):
        result = engine._run_step(page, click_step, {}, evidence, {"expect": False},
                                   outputs={}, artifact=MagicMock(artifact_id="a", version=1),
                                   run_id="r1", idx=0)

    assert result is not None
    assert result.status == ReplayStatus.FAILURE
    assert result.failure.step_id == "s1"


def test_run_step_extracts_output_value(tmp_path):
    engine = make_engine(tmp_path)
    evidence = make_evidence(tmp_path)
    step = Step(step_id="s2", action=ActionType.EXTRACT, extract_as="savings_balance",
                target=ElementTarget(primary=Locator(strategy=LocatorStrategy.CSS, value="td")))
    page = MagicMock()
    page.content.return_value = "<html>$4,820.55</html>"

    outputs = {}
    with patch("agent.replay.resolve", return_value=(FakeLocator(text="$4,820.55"), "css")):
        result = engine._run_step(page, step, {}, evidence, {"expect": False},
                                   outputs=outputs, artifact=MagicMock(artifact_id="a", version=1),
                                   run_id="r1", idx=0)

    assert result is None
    assert outputs["savings_balance"] == "$4,820.55"


def test_risky_action_requires_confirmation_blocks_by_default_policy(tmp_path):
    engine = make_engine(tmp_path, policy="block")
    assert engine.guardrails.requires_confirmation(RiskLevel.RISKY, confirmed=True) is True


def test_guardrail_blocks_domain_outside_allowlist(tmp_path):
    from agent.guardrails import GuardrailViolation
    engine = make_engine(tmp_path, allowed_domains=["127.0.0.1:5055"])
    with pytest.raises(GuardrailViolation):
        engine.guardrails.check_domain("https://not-allowed.example.com/x")


def test_navigation_step_checks_rendered_url_against_allowlist(tmp_path):
    from agent.guardrails import GuardrailViolation
    engine = make_engine(tmp_path, allowed_domains=["127.0.0.1:5055"])
    evidence = make_evidence(tmp_path)
    step = Step(step_id="s0", action=ActionType.NAVIGATE,
                url_template="https://evil.example/member/{{member_id}}")
    page = MagicMock()
    artifact = MagicMock(artifact_id="a", version=1)

    result = engine._run_step(page, step, {"member_id": "12345"}, evidence,
                              {"expect": False}, outputs={}, artifact=artifact,
                              run_id="r1", idx=0)
    assert result is not None
    assert result.status == ReplayStatus.FAILURE
    assert not page.goto.called
