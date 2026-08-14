# Computer-Use Automation System — interface.ai take-home

A backend integration layer that lets an AI agent operate a legacy,
no-API back-office web app: an LLM figures out how to accomplish a goal by
driving the real UI once (**discovery**), that run is compiled into a
typed, versioned, agent-invocable **artifact**, and the artifact then
**replays deterministically** — no model in the loop — with explicit
handling of business outcomes, recoverable conditions, and hard failures,
plus a real human-escalation/handoff path.

See `/REPORT.md` for the design write-up (architecture, schema, error
handling, heterogeneity/multi-tenant story, escalation, safety, cuts).

## What's here

```
target_app/       the proxy target: a deliberately legacy Flask app
                   (Meridian Credit Union member servicing console) -
                   table layout, no test IDs, a native confirm() dialog,
                   and real runtime conditions (not-found, permission
                   denied, transient failure, validation error).
agent/            the automation system itself
  schemas.py       the Artifact contract (locators, params, outputs, checkpoint, risk)
  dom_observer.py  DOM/accessibility-grounded controls + read-only data observation + locator generation
  locator_resolver.py   resolves a Locator -> live Playwright element, with fallbacks
  llm_client.py    Gemini function-calling adapter (discovery ONLY)
  agent_loop.py    discovery loop: observe -> decide -> act -> compile artifact
  replay.py        deterministic replay engine + error taxonomy
  guardrails.py    allowlist, risk policy, redaction
  escalation.py    session/control-transfer state machine
  evidence.py      structured logs + screenshots
operator_console/  CLI operator console - attaches to the SAME live browser via CDP
cli.py             `discover` / `replay` / `list` commands
config/allowlist.yaml   the guardrail policy used for this target app
tests/             unit tests (no browser required)
artifacts/         saved capability artifacts land here
evidence/          per-run structured logs + screenshots land here
sessions/          control-transfer state files (one per run)
```

## Setup

Requires Python 3.11+.

```bash
cd interface-ai-assignment      # this directory
pip install -r requirements.txt
python -m playwright install chromium   # downloads a Chromium binary (~150MB)
export GEMINI_API_KEY=...      # only needed for `discover`, not `replay`
```

### Gemini API key for discovery

Discovery uses **Gemini 3.5 Flash-Lite** through the Gemini Developer API. Create an
API key in Google AI Studio and set it only in your shell environment:

```powershell
$env:GEMINI_API_KEY="YOUR_KEY_HERE"
$env:GEMINI_MODEL="gemini-3.5-flash-lite" # optional; this is already the default
```

The key is never written to artifacts or evidence. At the time of submission,
Google lists Gemini 3.5 Flash-Lite text input and output as free of charge on
the Gemini API Free Tier, subject to Google's account, region, and rate-limit
availability. No Google Search/Maps grounding or paid Computer Use model is used.
`agent/llm_client.py` is isolated behind a single `decide()` seam so another
provider can be substituted without changing the discovery loop or replay engine.

Start the target app in one terminal and leave it running:

```bash
python target_app/app.py        # serves http://127.0.0.1:5055
```

The app has one fixed operator login (`operator` / `demo123`) and three
seeded members: `12345` (normal), `55555` (triggers one transient failure
before succeeding on retry), `00042` (restricted — triggers a permission
denial). Any other ID is "not found".

> **Note on login:** each discovery/replay run launches a brand-new browser
> context, so there's no session cookie to inherit. Rather than making
> "how to log in" part of every recorded capability, `agent/auth.py`
> authenticates the context once before the recorded/replayed steps begin —
> conceptually the same role SSO/a service-account token plays against a
> real core banking system. `--entry-url` below can point straight at the
> target screen; you don't need to route it through `/login` yourself.
> See REPORT.md "Cuts" for why this was the right scope line.


### Windows / VS Code quick start

From a PowerShell terminal opened at the repository root:

```powershell
py -3.11 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
python -m pytest tests -v
```

If you use Anaconda instead of `venv`, the equivalent setup is:

```powershell
conda create -n interface-ai python=3.11 -y
conda activate interface-ai
pip install -r requirements.txt
python -m playwright install chromium
python -m pytest tests -v
```

Use one VS Code terminal for `python target_app/app.py` and a second terminal
(with the same environment activated) for the `discover` / `replay` commands below.
For a visible browser during the evidence run, add `--headed`.

## Demo path

**1. Discovery run** (needs `GEMINI_API_KEY`; opens a real browser and calls live Gemini 3.5 Flash-Lite to complete a goal it has never seen before):

```bash
python cli.py discover --flow member_lookup \
  --goal "Look up member 12345 and read their savings and checking balance, then confirm you can see the member record" \
  --entry-url http://127.0.0.1:5055/member/12345 \
  --param member_id=12345
```

This writes a structured log + screenshots to `evidence/discovery_<run_id>/`
and, on success, a capability artifact to `artifacts/meridian.member_lookup_v1.json`.

A second, more interesting flow (multi-field form + a native `confirm()`
dialog + a risky final step):

```bash
python cli.py discover --flow open_subaccount \
  --goal "Open a new Share Savings sub-account for member 67890 with a $500.00 initial deposit, and reach the confirmation screen showing the new account number" \
  --entry-url http://127.0.0.1:5055/member/67890/new-subaccount \
  --param member_id=67890 --param account_type=share --param initial_deposit=500
```

**2. Deterministic replay** (no API key, no model — this is the production
path an AI agent would trigger):

```bash
python cli.py replay --artifact artifacts/meridian.member_lookup_v1.json \
  --param member_id=12345
```

Try it against a member that doesn't exist to see the **business outcome**
path (not a crash):

```bash
python cli.py replay --artifact artifacts/meridian.member_lookup_v1.json \
  --param member_id=00000
```

And the risky-step confirmation gate on the sub-account flow:

```bash
# without --confirm-risky, the risky "submit" step pauses and raises an
# intervention request instead of executing unattended
python cli.py replay --artifact artifacts/meridian.open_subaccount_v1.json \
  --param member_id=67890 --param account_type=share --param initial_deposit=500
```

While that command is blocked waiting on human confirmation, run the
operator console in a second terminal, attached to the **same live
session** (the session ID is printed by the blocked replay run, and also
discoverable as the newest file under `sessions/`):

```bash
python -m operator_console.console replay-<run_id>
operator> look
operator> resume confirmed by supervisor
```

The replay run then continues on the same page and finishes.

**3. List saved artifacts:**

```bash
python cli.py list
```

## Running without live services

The unit test suite (`tests/`) exercises the artifact schema, guardrails,
redaction, templating, locator-generation, and the replay engine's control
flow (business outcome / recoverable-retry / hard-failure branching) with a
mocked page — no browser, no API key, no target app required:

```bash
pip install pytest
PYTHONPATH=. pytest tests/ -v
```

## A note on this sandbox vs. your machine

This repository was built and unit-tested in a network-restricted sandbox
that could install all Python dependencies but could not download a
Chromium binary (Playwright's CDN wasn't reachable) or reach a live model API. So while the target app was verified end-to-end with real HTTP
requests and the full logic layer is covered by 30 passing unit/mock tests,
**the actual discovery + replay runs against a live browser need to be
executed on a machine with normal internet access** — that's what
`playwright install chromium` and the commands above are for. `/evidence/`
will be populated the first time you run them.
