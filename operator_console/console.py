"""
Operator console (MOCK UI, REAL MECHANISM).

Per the brief's scope note, a full real-time co-browsing console is out of
scope. What's real here: this is a *separate process* from the automation
run. It attaches to the exact same live browser via
`chromium.connect_over_cdp(cdp_endpoint)` - not a fresh session, not a
replica - reads the pending intervention request, lets a human issue
actions against the live page, records every action taken, and then hands
control back so the paused run resumes on the same session.

Usage:
    python -m operator_console.console <session_id>

Commands once attached:
    look                       re-observe the page (list interactive elements)
    click <ref>                click element by ref (e.g. e3)
    fill <ref> <text>          fill a text input
    select <ref> <option text> choose a <select> option
    screenshot                 save a screenshot to evidence/manual/
    resume [note]              hand control back to automation and exit
"""
from __future__ import annotations
import sys
import shlex
from pathlib import Path

from playwright.sync_api import sync_playwright

from agent.escalation import SessionStore
from agent.dom_observer import observe


def main():
    if len(sys.argv) < 2:
        print("usage: python -m operator_console.console <session_id>")
        sys.exit(1)
    session_id = sys.argv[1]
    store = SessionStore("sessions")
    data = store.read(session_id)
    if data is None:
        print(f"No such session: {session_id}")
        sys.exit(1)

    print(f"=== Operator console attaching to session {session_id} ===")
    print(f"CDP endpoint : {data['cdp_endpoint']}")
    print(f"Control owner: {data['control_owner']}")
    ir = data.get("intervention_request")
    if ir:
        print(f"Reason       : {ir['reason']}")
        print(f"Context      : {ir['step_context']}")
        print(f"Screenshot   : {ir.get('screenshot_path')}")
    else:
        print("(no pending intervention request on file - session may already be resolved)")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(data["cdp_endpoint"])
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        print(f"Attached to live page: {page.url}\n")

        while True:
            try:
                raw = input("operator> ").strip()
            except (EOFError, KeyboardInterrupt):
                raw = "resume"
            if not raw:
                continue
            parts = shlex.split(raw)
            cmd = parts[0].lower()

            if cmd == "look":
                obs = observe(page)
                print(obs.to_prompt())

            elif cmd == "click" and len(parts) >= 2:
                ref = parts[1]
                obs = observe(page)
                el = next((e for e in obs.elements if e.ref == ref), None)
                if not el:
                    print(f"no such ref: {ref}")
                    continue
                from agent.dom_observer import build_element_target
                from agent.schemas import ElementTarget
                from agent.locator_resolver import resolve
                locator, strat = resolve(page, ElementTarget(**build_element_target(el)))
                locator.click()
                store.record_human_action(session_id, {"type": "click", "ref": ref, "strategy": strat})
                print(f"clicked {ref} via {strat}")

            elif cmd == "fill" and len(parts) >= 3:
                ref, text = parts[1], " ".join(parts[2:])
                obs = observe(page)
                el = next((e for e in obs.elements if e.ref == ref), None)
                if not el:
                    print(f"no such ref: {ref}")
                    continue
                from agent.dom_observer import build_element_target
                from agent.schemas import ElementTarget
                from agent.locator_resolver import resolve
                locator, strat = resolve(page, ElementTarget(**build_element_target(el)))
                locator.fill(text)
                store.record_human_action(session_id, {"type": "fill", "ref": ref, "strategy": strat,
                                                         "value_redacted": True})
                print(f"filled {ref} via {strat}")

            elif cmd == "select" and len(parts) >= 3:
                ref, opt = parts[1], " ".join(parts[2:])
                obs = observe(page)
                el = next((e for e in obs.elements if e.ref == ref), None)
                if not el:
                    print(f"no such ref: {ref}")
                    continue
                from agent.dom_observer import build_element_target
                from agent.schemas import ElementTarget
                from agent.locator_resolver import resolve
                locator, strat = resolve(page, ElementTarget(**build_element_target(el)))
                locator.select_option(label=opt)
                store.record_human_action(session_id, {"type": "select", "ref": ref, "option": opt, "strategy": strat})
                print(f"selected '{opt}' on {ref} via {strat}")

            elif cmd == "screenshot":
                out = Path("evidence/manual")
                out.mkdir(parents=True, exist_ok=True)
                path = out / f"{session_id}_{len(list(out.glob('*.png')))}.png"
                page.screenshot(path=str(path))
                store.record_human_action(session_id, {"type": "screenshot", "path": str(path)})
                print(f"saved {path}")

            elif cmd == "resume":
                note = " ".join(parts[1:]) if len(parts) > 1 else "resolved by operator"
                store.resume_to_automation(session_id, resolution_note=note)
                print(f"Control handed back to automation. Note: {note}")
                break

            else:
                print("unknown command. try: look | click <ref> | fill <ref> <text> | "
                      "select <ref> <option> | screenshot | resume [note]")


if __name__ == "__main__":
    main()
