#!/usr/bin/env python3
"""
CLI entrypoint.

  Discover a new capability (requires GEMINI_API_KEY):
    python cli.py discover --flow member_lookup \
        --goal "Look up member 12345 and read their savings and checking balance" \
        --entry-url http://127.0.0.1:5055/search \
        --param member_id=12345

    python cli.py discover --flow open_subaccount \
        --goal "Open a new Share Savings sub-account for member 67890 with a $500 initial deposit, and reach the confirmation screen" \
        --entry-url http://127.0.0.1:5055/member/67890/new-subaccount \
        --param member_id=67890 --param account_type=share --param initial_deposit=500

  Replay a saved artifact deterministically (no LLM, no API key needed):
    python cli.py replay --artifact artifacts/meridian.member_lookup_v1.json \
        --param member_id=12345

    python cli.py replay --artifact artifacts/meridian.open_subaccount_v1.json \
        --param member_id=67890 --param account_type=share --param initial_deposit=500 \
        --confirm-risky

  List saved artifacts:
    python cli.py list
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from agent.agent_loop import DiscoveryLoop
from agent.replay import ReplayEngine
from agent.schemas import Artifact

APP_ID = "meridian"
ALLOWLIST = "config/allowlist.yaml"


def parse_params(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--param must be key=value, got: {p}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def cmd_discover(args):
    loop = DiscoveryLoop(ALLOWLIST, headless=args.headed is False)
    params = parse_params(args.param)
    artifact, evidence_dir = loop.run(
        goal=args.goal,
        entry_url=args.entry_url,
        app_id=APP_ID,
        flow_id=args.flow,
        input_params=params,
    )
    print(f"\nEvidence written to: {evidence_dir}")
    if artifact:
        print(f"Artifact saved: artifacts/{artifact.artifact_id}_v{artifact.version}.json")
        print(json.dumps(json.loads(artifact.model_dump_json()), indent=2)[:2000])
    else:
        print("Discovery run did NOT produce an artifact (goal not reached - see evidence log).")
        sys.exit(2)


def cmd_replay(args):
    artifact = Artifact.model_validate_json(Path(args.artifact).read_text())
    engine = ReplayEngine(ALLOWLIST, headless=args.headed is False)
    params = parse_params(args.param)
    result = engine.replay(artifact, params, confirm_risky=args.confirm_risky,
                            require_approved=args.require_approved)
    print(json.dumps(json.loads(result.model_dump_json()), indent=2))
    if result.status == "failure":
        sys.exit(1)


def cmd_list(args):
    root = Path("artifacts")
    for p in sorted(root.glob("*.json")):
        a = Artifact.model_validate_json(p.read_text())
        print(f"{a.artifact_id:35s} v{a.version}  [{a.status}]  {p.name}")
        print(f"    goal: {a.goal_template}")
        print(f"    params: {[ip.name for ip in a.input_params]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover")
    d.add_argument("--goal", required=True)
    d.add_argument("--entry-url", required=True)
    d.add_argument("--flow", required=True, choices=["member_lookup", "open_subaccount"])
    d.add_argument("--param", action="append", help="key=value, repeatable")
    d.add_argument("--headed", action="store_true", help="run browser headed (needs a display)")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("replay")
    r.add_argument("--artifact", required=True)
    r.add_argument("--param", action="append", help="key=value, repeatable")
    r.add_argument("--confirm-risky", action="store_true")
    r.add_argument("--require-approved", action="store_true")
    r.add_argument("--headed", action="store_true")
    r.set_defaults(func=cmd_replay)

    l = sub.add_parser("list")
    l.set_defaults(func=cmd_list)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
