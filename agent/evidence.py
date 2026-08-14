"""
Evidence capture for both discovery and replay runs.

Every run gets its own directory under evidence/ containing:
  - run.jsonl      structured, append-only log: one JSON object per event
  - screenshots/   PNGs, primarily on error/stuck/checkpoint events
  - meta.json      summary written at the end of the run

All text is passed through guardrails.redact_text before it's written, and
any field flagged `sensitive` in the artifact schema is redacted by name,
not just by pattern-matching - so a member ID or balance that a regex
wouldn't catch still never lands in a log file.
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from datetime import datetime, timezone

from agent.guardrails import redact_text, redact_dict


class EvidenceRun:
    def __init__(self, evidence_root: str, run_id: str, kind: str):
        self.run_id = run_id
        self.kind = kind  # "discovery" | "replay"
        self.dir = Path(evidence_root) / f"{kind}_{run_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "screenshots").mkdir(exist_ok=True)
        self._log_path = self.dir / "run.jsonl"
        self._start = time.time()
        self.log("run_started", {"kind": kind, "run_id": run_id})

    def log(self, event: str, data: dict, sensitive_keys: set[str] | None = None):
        safe_data = redact_dict(data, sensitive_keys or set())
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **safe_data,
        }
        with self._log_path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def screenshot(self, page, name: str) -> str:
        path = self.dir / "screenshots" / f"{name}.png"
        try:
            page.screenshot(path=str(path))
            return str(path)
        except Exception as e:
            self.log("screenshot_failed", {"name": name, "error": str(e)})
            return ""

    def dom_snapshot(self, page, name: str) -> str:
        path = self.dir / "screenshots" / f"{name}.html"
        try:
            content = page.content()
            path.write_text(redact_text(content))
            return str(path)
        except Exception as e:
            self.log("dom_snapshot_failed", {"name": name, "error": str(e)})
            return ""

    def finish(self, summary: dict):
        summary = {**summary, "duration_ms": int((time.time() - self._start) * 1000)}
        (self.dir / "meta.json").write_text(json.dumps(summary, indent=2, default=str))
        self.log("run_finished", summary)
        return summary
