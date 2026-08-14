"""Gemini decision client used only during discovery.

The discovery loop uses Google's Gemini Developer API with Gemini 3.5 Flash-Lite.
The rest of the system depends only on this tiny ``decide`` interface, and
replay never imports this module: after discovery, the saved artifact is the
source of truth and execution is deterministic.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

SYSTEM_PROMPT = """You are a computer-use agent operating an internal bank \
servicing web application on behalf of an automated backend integration \
system. You will be shown the current page as a URL, title, any notable \
banner text, visible page text, and a list of both interactive and readable elements with short refs like [e3].

Your job: choose exactly ONE next action per turn using the `agent_action` \
function, to make progress toward the stated goal. You do not see pixels - you \
only see the structured observation, so use an element ref when an action targets a control or readable value; never guess coordinates or markup.

Rules:
- Only act on elements that are present in the current observation. Click/fill/select only interactive elements; extract may target readable text elements such as table cells.
- Prefer the most direct path to the goal; don't explore unrelated pages.
- If the goal asks you to read/report specific data (e.g. a balance), use \
action="extract" on the readable element showing that exact value, with `extract_field_name` \
set to a short snake_case name - do this BEFORE calling done, not instead of it.
- If a form submit is expected to trigger a native browser confirm() dialog, \
set expect_dialog=true so the harness can handle it.
- When you believe the goal has been fully achieved (you can see the data \
you were asked to find, or the confirmation screen you were asked to reach), \
FIRST call the function with action="assert_text" and a `value` set to a short, \
distinctive, exact substring of text that proves you reached this state \
(e.g. "Sub-Account Opened Successfully" or "Member Record: 12345") - this \
becomes the reusable checkpoint the replay engine checks on every future run. \
THEN, on your next turn, call the function with action="done" and fill in \
`outputs` and `checkpoint_description`.
- If you hit a wall you cannot safely resolve yourself (e.g. an access-denied \
message, an unrecoverable error, ambiguity about a risky irreversible step), \
call the function with action="stuck" and explain why - a human will take over.
- Never fabricate data. If asked to extract a value, copy it exactly as shown.
"""

TOOL_SCHEMA = {
    "name": "agent_action",
    "description": "Choose the single next action to take on the current page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "action": {
                "type": "string",
                "enum": ["navigate", "click", "fill", "select", "extract", "assert_text", "done", "stuck"],
            },
            "ref": {"type": "string"},
            "value": {"type": "string"},
            "expect_dialog": {"type": "boolean"},
            "extract_field_name": {"type": "string"},
            "outputs": {"type": "object"},
            "checkpoint_description": {"type": "string"},
            "stuck_reason": {"type": "string"},
        },
        "required": ["reasoning", "action"],
    },
}

DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
VALID_ACTIONS = set(TOOL_SCHEMA["input_schema"]["properties"]["action"]["enum"])


class LLMClient:
    """Small Gemini adapter exposing one validated ``decide`` method."""

    provider = "gemini"

    def __init__(self, model: str | None = None):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Create a free-tier Gemini API key "
                "in Google AI Studio and set $env:GEMINI_API_KEY."
            )
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError(
                "google-genai is not installed. Run: pip install -r requirements.txt"
            ) from exc

        self.model = model or DEFAULT_GEMINI_MODEL
        self._types = types
        self.client = genai.Client(api_key=api_key)

        function = types.FunctionDeclaration(
            name=TOOL_SCHEMA["name"],
            description=TOOL_SCHEMA["description"],
            parameters_json_schema=TOOL_SCHEMA["input_schema"],
        )
        self._tool = types.Tool(function_declarations=[function])

    def decide(
        self,
        goal: str,
        target_params: dict,
        observation_text: str,
        history: list[dict],
    ) -> dict[str, Any]:
        """Return one validated action chosen by the live Gemini model."""
        history_text = self._format_history(history)
        user_msg = (
            f"GOAL: {goal}\n"
            f"AVAILABLE INPUT PARAMETERS: {json.dumps(target_params)}\n\n"
            f"{history_text}"
            f"CURRENT PAGE OBSERVATION:\n{observation_text}\n\n"
            "Choose the next action by calling agent_action exactly once."
        )

        types = self._types
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[self._tool],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY",
                    allowed_function_names=["agent_action"],
                )
            ),
            temperature=0.1,
            max_output_tokens=512,
        )

        response = None
        last_error = None
        for attempt in range(4):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=user_msg,
                    config=config,
                )
                break
            except Exception as exc:
                last_error = exc
                message = str(exc)
                transient = any(code in message for code in (
                    "429", "RESOURCE_EXHAUSTED", "500", "503", "UNAVAILABLE"
                ))
                if not transient:
                    raise RuntimeError(f"Gemini API call failed: {exc}") from exc
                if attempt < 3:
                    time.sleep(min(2 ** attempt, 8))

        if response is None:
            raise RuntimeError(
                f"Gemini API call failed after retries: {last_error}"
            )

        function_calls = response.function_calls or []
        if not function_calls:
            raise RuntimeError(
                "Gemini returned no agent_action function call. "
                f"text={getattr(response, 'text', None)!r}"
            )

        call = function_calls[0]
        if getattr(call, "name", None) != "agent_action":
            raise RuntimeError(f"unexpected Gemini function call: {getattr(call, 'name', None)!r}")

        args = getattr(call, "args", None)
        if args is None and hasattr(call, "function_call"):
            args = getattr(call.function_call, "args", None)
        if hasattr(args, "model_dump"):
            args = args.model_dump(exclude_none=True)
        if not isinstance(args, dict):
            try:
                args = dict(args)
            except Exception as exc:
                raise RuntimeError(
                    f"unexpected Gemini function arguments type: {type(args).__name__}"
                ) from exc

        return self._validate_decision(args)

    @staticmethod
    def _validate_decision(decision: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(decision, dict):
            raise RuntimeError("model decision must be an object")
        action = decision.get("action")
        if action not in VALID_ACTIONS:
            raise RuntimeError(f"invalid model action: {action!r}")
        if not isinstance(decision.get("reasoning"), str) or not decision["reasoning"].strip():
            raise RuntimeError("model decision is missing non-empty reasoning")

        if action in {"click", "fill", "select", "extract"} and not decision.get("ref"):
            raise RuntimeError(f"action {action!r} requires an element ref")
        if action in {"navigate", "fill", "select", "assert_text"} and decision.get("value") is None:
            raise RuntimeError(f"action {action!r} requires a value")
        if action == "extract" and not decision.get("extract_field_name"):
            raise RuntimeError("extract requires extract_field_name")
        if action == "done":
            if not isinstance(decision.get("outputs"), dict):
                raise RuntimeError("done requires an outputs object")
            if not decision.get("checkpoint_description"):
                raise RuntimeError("done requires checkpoint_description")
        if action == "stuck" and not decision.get("stuck_reason"):
            raise RuntimeError("stuck requires stuck_reason")

        return decision

    @staticmethod
    def _format_history(history: list[dict]) -> str:
        if not history:
            return ""
        lines = ["PRIOR STEPS THIS RUN:"]
        for i, h in enumerate(history[-8:], 1):
            action = h["action"]
            lines.append(
                f"  {i}. action={action.get('action')} ref={action.get('ref')} "
                f"value={action.get('value')} -> {h.get('result_summary', '')}"
            )
        lines.append("")
        return "\n".join(lines)
