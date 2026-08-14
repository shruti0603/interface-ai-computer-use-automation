"""
Safety & policy guardrails.

Two independent concerns live here, deliberately kept separate:

1. Allowlist enforcement - WHERE the agent/replay is permitted to act at all
   (domains + action types). This is a hard boundary checked before every
   navigation and every action, in both discovery and replay.

2. Risk policy - WHAT to do about actions that are individually permitted
   but state-changing/irreversible. Safe/reversible actions execute freely;
   risky actions are handled conservatively per RiskPolicy (see below).

Redaction is also here since it's a safety concern: never let secrets or raw
PII reach an artifact or a log line.
"""
from __future__ import annotations
import re
import fnmatch
from dataclasses import dataclass, field
from urllib.parse import urlparse
from pathlib import Path
import yaml

from agent.schemas import RiskLevel, ActionType


class GuardrailViolation(Exception):
    """Raised when an action would step outside the allowlist. Always a hard
    stop - never silently ignored or downgraded."""


@dataclass
class AllowlistConfig:
    allowed_domains: list[str]              # glob patterns, e.g. "127.0.0.1:*"
    allowed_action_types: list[str]         # subset of ActionType values
    risky_action_policy: str = "require_confirmation"  # block | require_confirmation | allow_with_flag
    max_steps: int = 40
    max_run_seconds: int = 180

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AllowlistConfig":
        data = yaml.safe_load(Path(path).read_text())
        return cls(**data)


class Guardrails:
    def __init__(self, config: AllowlistConfig):
        self.config = config

    # -- allowlist -----------------------------------------------------
    def check_domain(self, url: str) -> None:
        host = urlparse(url).netloc or url
        for pattern in self.config.allowed_domains:
            if fnmatch.fnmatch(host, pattern):
                return
        raise GuardrailViolation(
            f"URL '{url}' (host '{host}') is not in the allowlist: "
            f"{self.config.allowed_domains}"
        )

    def check_action_type(self, action: ActionType) -> None:
        if action.value not in self.config.allowed_action_types:
            raise GuardrailViolation(
                f"Action type '{action.value}' is not in the allowed action "
                f"types: {self.config.allowed_action_types}"
            )

    # -- risk policy -----------------------------------------------------
    def classify_action_type(self, action: ActionType) -> RiskLevel:
        """Default classification an artifact author can override per-step.
        Reads/navigation are safe; anything that submits data is at least
        reversible; the discovery/authoring layer or a human reviewer should
        bump specific steps (e.g. a final "Open Sub-Account" submit) to
        RISKY explicitly - see REPORT.md Safety section for why we don't try
        to infer "irreversible" purely from the action type."""
        if action in (ActionType.NAVIGATE, ActionType.WAIT_FOR, ActionType.EXTRACT, ActionType.ASSERT_TEXT):
            return RiskLevel.SAFE
        if action in (ActionType.FILL, ActionType.SELECT):
            return RiskLevel.REVERSIBLE
        return RiskLevel.REVERSIBLE  # CLICK defaults reversible; steps override to RISKY explicitly

    def requires_confirmation(self, risk: RiskLevel, confirmed: bool) -> bool:
        """Returns True if this step must pause for explicit confirmation
        (human approval or an explicit `confirm_risky=True` invocation
        argument) before executing."""
        if risk != RiskLevel.RISKY:
            return False
        policy = self.config.risky_action_policy
        if policy == "block":
            return True  # never proceeds unattended; caller must escalate
        if policy == "require_confirmation":
            return not confirmed
        if policy == "allow_with_flag":
            return False
        raise ValueError(f"unknown risky_action_policy: {policy}")


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_REDACT_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                 # SSN-shaped
    re.compile(r"\b\d{9,16}\b"),                           # account/card-shaped numeric runs
    re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|bearer)\s*[:=]\s*\S+"),
]

REDACTED = "[REDACTED]"


def redact_text(text: str) -> str:
    if not text:
        return text
    out = text
    for pat in _REDACT_PATTERNS:
        out = pat.sub(REDACTED, out)
    return out


def redact_value_for_field(name: str, value, sensitive: bool) -> str:
    """Used when logging/serializing a specific named field whose sensitivity
    is known from the artifact schema (input_params[].sensitive /
    outputs[].sensitive), rather than pattern-guessing."""
    if sensitive:
        return REDACTED
    return redact_text(str(value)) if isinstance(value, str) else value


def redact_dict(d: dict, sensitive_keys: set[str]) -> dict:
    out = {}
    for k, v in d.items():
        if k in sensitive_keys:
            out[k] = REDACTED
        elif isinstance(v, str):
            out[k] = redact_text(v)
        else:
            out[k] = v
    return out
