"""
Core data contracts.

The Artifact is the one object that matters most in this system: it's what
turns a one-off LLM-discovered flow into a reusable, reviewable capability
that an AI agent can invoke without a model in the loop. Everything else
(discovery loop, replay engine, guardrails) reads or writes this shape.
"""
from __future__ import annotations
from enum import Enum
from typing import Optional, Any, Literal
from pydantic import BaseModel, Field
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Locators
# ---------------------------------------------------------------------------

class LocatorStrategy(str, Enum):
    """
    Ordered by robustness, most stable first. Replay tries the primary
    strategy, and on failure falls through `fallbacks` in order before
    declaring a hard failure. This is the seam that lets the same artifact
    survive minor UI drift (a class name changing, a table gaining a column)
    without a re-record.
    """
    TEST_ID = "test_id"                # data-testid / data-qa - essentially never present in this environment
    ROLE_NAME = "role_name"            # accessibility role + accessible name (aria-label, <label>, associated text)
    FORM_FIELD_NAME = "form_field_name"  # HTML `name` attribute - common even in legacy server-rendered forms
    LABEL_TEXT = "label_text"          # nearest preceding <td>/<label> text in a table/form layout
    EXACT_TEXT = "exact_text"          # visible text of a link/button, exact match
    CSS = "css"                        # CSS selector (last resort - most brittle to markup changes)
    XPATH = "xpath"


class Locator(BaseModel):
    strategy: LocatorStrategy
    value: str
    # human-readable description of *what* this targets, independent of how -
    # lets a reviewer sanity check the capability without knowing selector syntax
    description: str = ""


class ElementTarget(BaseModel):
    """A target element/control with a primary locator and ranked fallbacks."""
    primary: Locator
    fallbacks: list[Locator] = Field(default_factory=list)
    frame_path: list[str] = Field(default_factory=list)  # for iframe/frameset nesting; empty = main frame


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"
    ASSERT_TEXT = "assert_text"


class RiskLevel(str, Enum):
    SAFE = "safe"           # read-only / trivially reversible (navigate, search, extract)
    REVERSIBLE = "reversible"  # writes but easily undone / low blast radius
    RISKY = "risky"          # state-changing, hard to reverse, or financially/materially significant


class KnownOutcome(BaseModel):
    """
    A named, expected business result that this step (or the run as a whole)
    can legitimately produce and that the caller needs to see - NOT an error.
    e.g. "no such member" after a search step.
    """
    code: str
    message_template: str
    matcher: Locator  # how we detect this outcome occurred (e.g. text banner)


class RecoverableCondition(BaseModel):
    """
    A transient/known interstitial the replay engine should handle inline
    (dismiss, wait-and-retry) rather than treating as a hard failure.
    """
    code: str
    matcher: Locator
    strategy: Literal["retry_step", "dismiss_and_retry", "wait_and_reload"] = "retry_step"
    max_retries: int = 2
    backoff_ms: int = 800


class Step(BaseModel):
    step_id: str
    action: ActionType
    target: Optional[ElementTarget] = None
    # value may reference input params via {{param_name}} templating
    value: Optional[str] = None
    url_template: Optional[str] = None       # for NAVIGATE
    extract_as: Optional[str] = None         # output name this EXTRACT step feeds
    expect_dialog: bool = False              # set True if this action triggers a native confirm()/alert()
    dialog_action: Literal["accept", "dismiss"] = "accept"
    timeout_ms: int = 8000
    risk_level: RiskLevel = RiskLevel.SAFE
    known_outcomes: list[KnownOutcome] = Field(default_factory=list)
    recoverable_conditions: list[RecoverableCondition] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Params / outputs / checkpoint
# ---------------------------------------------------------------------------

class ParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class InputParam(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str = ""
    # redact this value in all logs/evidence (e.g. would-be PII in a real system)
    sensitive: bool = False
    default: Optional[Any] = None


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str = ""
    source_step_id: str          # which EXTRACT step produced this
    sensitive: bool = False


class Checkpoint(BaseModel):
    """Asserted at the end of replay to confirm the run actually reached the
    declared success state, rather than trusting that the last click worked."""
    description: str
    target: ElementTarget


# ---------------------------------------------------------------------------
# Target / allowlist scope
# ---------------------------------------------------------------------------

class TargetScope(BaseModel):
    app_id: str
    entry_url: str
    allowed_domains: list[str]          # subset of the global allowlist this artifact needs
    tenant_id: Optional[str] = None     # None = tenant-agnostic / "base" recording (see REPORT.md 4.)


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------

class ArtifactStatus(str, Enum):
    DRAFT = "draft"          # produced by discovery, not yet reviewed
    APPROVED = "approved"    # human-reviewed, eligible for unattended replay
    DEPRECATED = "deprecated"


class Artifact(BaseModel):
    model_config = {"protected_namespaces": ()}

    artifact_id: str
    name: str
    version: int = 1
    status: ArtifactStatus = ArtifactStatus.DRAFT
    description: str
    goal_template: str  # the natural-language goal this capability satisfies, for the catalog

    target: TargetScope
    input_params: list[InputParam]
    steps: list[Step]
    outputs: list[OutputField]
    checkpoint: Checkpoint

    created_at: str = Field(default_factory=now_iso)
    created_from_run_id: str = ""
    model_used: str = ""

    def required_params(self) -> list[str]:
        return [p.name for p in self.input_params if p.required]


# ---------------------------------------------------------------------------
# Replay result contract
# ---------------------------------------------------------------------------

class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"


class FailureDetail(BaseModel):
    step_id: str
    step_index: int
    expected: str
    observed: str
    screenshot_path: Optional[str] = None
    dom_snapshot_path: Optional[str] = None


class ReplayResult(BaseModel):
    status: ReplayStatus
    artifact_id: str
    artifact_version: int
    run_id: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    business_outcome_code: Optional[str] = None
    business_outcome_message: Optional[str] = None
    failure: Optional[FailureDetail] = None
    evidence_dir: str = ""
    duration_ms: int = 0
