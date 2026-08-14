"""
Human-in-the-loop escalation and control handoff.

The core idea: automation and a (possibly separate-process) human operator
share ONE live browser session via the Chrome DevTools Protocol endpoint the
browser was launched with. Control is a simple state machine persisted to a
small JSON file per session, so either side can be a completely different
OS process:

    AUTOMATION ---(stuck / risky step needs confirmation)---> HUMAN
    HUMAN       ---(operator issues `resume`)----------------> AUTOMATION

While control=human, the automation loop blocks on wait_for_resume() instead
of touching the page - it does not race the operator for control of the
session. The operator console (operator_console/console.py) attaches to the *same*
page via connect_over_cdp(), so nothing is thrown away and no fresh session
is created: this is what makes it a real handoff and not just a pause.

A full real-time co-browsing UI is out of scope (per the brief) - the
operator surface here is a CLI, but the state machine, the CDP attach, and
the evidence trail it produces are real, not mocked.
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional


class ControlOwner:
    AUTOMATION = "automation"
    HUMAN = "human"


class RunStatus:
    RUNNING = "running"
    PAUSED_FOR_HUMAN = "paused_for_human"
    RESUMED = "resumed"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class SessionStore:
    """File-backed session/control state. One JSON file per session under
    sessions/<session_id>.json. Writes are atomic (write to temp + rename)
    so a concurrent reader never sees a half-written file."""

    def __init__(self, sessions_root: str = "sessions"):
        self.root = Path(sessions_root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def _write(self, session_id: str, data: dict):
        path = self._path(session_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        os.replace(tmp, path)

    def read(self, session_id: str) -> Optional[dict]:
        path = self._path(session_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def create(self, session_id: str, cdp_endpoint: str, page_url: str,
               artifact_id: str = "", goal: str = "") -> dict:
        data = {
            "session_id": session_id,
            "cdp_endpoint": cdp_endpoint,
            "page_url": page_url,
            "artifact_id": artifact_id,
            "goal": goal,
            "control_owner": ControlOwner.AUTOMATION,
            "status": RunStatus.RUNNING,
            "intervention_request": None,
            "human_actions": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._write(session_id, data)
        return data

    def request_intervention(self, session_id: str, reason: str, step_context: dict,
                              screenshot_path: str = "") -> dict:
        data = self.read(session_id)
        if data is None:
            raise KeyError(f"unknown session {session_id}")
        data["control_owner"] = ControlOwner.HUMAN
        data["status"] = RunStatus.PAUSED_FOR_HUMAN
        data["intervention_request"] = {
            "reason": reason,
            "step_context": step_context,
            "screenshot_path": screenshot_path,
            "requested_at": time.time(),
        }
        data["updated_at"] = time.time()
        self._write(session_id, data)
        return data

    def record_human_action(self, session_id: str, action: dict) -> dict:
        data = self.read(session_id)
        data["human_actions"].append({**action, "ts": time.time()})
        data["updated_at"] = time.time()
        self._write(session_id, data)
        return data

    def resume_to_automation(self, session_id: str, resolution_note: str = "") -> dict:
        data = self.read(session_id)
        data["control_owner"] = ControlOwner.AUTOMATION
        data["status"] = RunStatus.RESUMED
        if data["intervention_request"] is not None:
            data["intervention_request"]["resolution_note"] = resolution_note
            data["intervention_request"]["resolved_at"] = time.time()
        data["updated_at"] = time.time()
        self._write(session_id, data)
        return data

    def complete(self, session_id: str) -> dict:
        data = self.read(session_id)
        data["status"] = RunStatus.COMPLETED
        data["updated_at"] = time.time()
        self._write(session_id, data)
        return data


def wait_for_resume(store: SessionStore, session_id: str, evidence,
                     poll_interval: float = 1.0, timeout_seconds: float = 300) -> dict:
    """Blocks (polling) until a human hands control back to automation, or
    timeout_seconds elapses (then raises TimeoutError - a hard failure the
    caller should surface, not silently drop). This is the seam that lets
    the loop 'pause, cede control, and resume on the same session'."""
    deadline = time.time() + timeout_seconds
    evidence.log("waiting_for_human", {"session_id": session_id, "timeout_seconds": timeout_seconds})
    while time.time() < deadline:
        data = store.read(session_id)
        if data["control_owner"] == ControlOwner.AUTOMATION:
            evidence.log("control_returned_to_automation", {
                "human_actions_taken": len(data["human_actions"]),
                "resolution_note": (data.get("intervention_request") or {}).get("resolution_note", ""),
            })
            return data
        time.sleep(poll_interval)
    raise TimeoutError(f"No human response for session {session_id} within {timeout_seconds}s")
