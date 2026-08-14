# Computer-Use Automation System — interface.ai Take-Home

A backend integration layer that lets an AI agent operate a legacy, no-API
back-office web application.

The system separates execution into two phases:

1. **Discovery** — an LLM observes and operates the real UI to accomplish a
   previously unseen goal.
2. **Replay** — the successful discovery is compiled into a typed, versioned
   capability artifact that can later execute deterministically with **no LLM
   in the replay loop**.

The implementation also includes explicit handling for business outcomes,
recoverable conditions, hard failures, safety guardrails, evidence capture,
and human escalation.

See [`REPORT.md`](REPORT.md) for the detailed architecture and design
discussion.

---

## Project structure

```text
target_app/             Deliberately legacy Flask application used as the target
agent/
  schemas.py            Artifact contract and typed execution models
  dom_observer.py       DOM/accessibility + read-only content observation
  locator_resolver.py   Locator resolution with fallback strategies
  llm_client.py         Gemini function-calling adapter used only for discovery
  agent_loop.py         LLM-driven discovery loop
  replay.py             Deterministic replay engine
  guardrails.py         Allowlist, risk policy, and redaction
  escalation.py         Human intervention/control-transfer state machine
  evidence.py           Structured evidence logs and screenshots

operator_console/       CLI for human intervention on the same browser session
config/                 Target allowlist and safety configuration
artifacts/              Generated capability artifacts
evidence/               Discovery and replay evidence
sessions/               Human-escalation session state
tests/                  Unit and replay-control-flow tests
cli.py                  discover / replay / list commands
REPORT.md               Architecture and design report
```

---

## Requirements

- Python 3.11+
- Chromium through Playwright
- Gemini API key for **discovery only**

Replay does **not** require an API key or an LLM.

---

## Setup

### Windows / VS Code with Conda

Create and activate an environment:

```powershell
conda create -n interface-ai python=3.11 -y
conda activate interface-ai
```

Install the dependencies:

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

Run the tests:

```powershell
python -m pytest tests -v
```

---

## Gemini API key

Discovery uses Gemini through the Gemini Developer API.

Create a Gemini API key in Google AI Studio and load it into the shell
environment.

In PowerShell:

```powershell
$env:GEMINI_API_KEY="YOUR_KEY_HERE"
```

Optionally, the model can be overridden with:

```powershell
$env:GEMINI_MODEL="gemini-3.5-flash-lite"
```

The API key is read from the environment and is **not stored in the artifact,
evidence logs, or repository**.

The LLM integration is isolated behind `agent/llm_client.py`, so another model
provider could be substituted without changing the replay engine.

---

## Start the target application

The proxy target is a deliberately legacy-style Flask application representing
a credit-union member-servicing console.

Start it in **Terminal 1**:

```powershell
conda activate interface-ai
python target_app/app.py
```

The application runs at:

```text
http://127.0.0.1:5055
```

Leave this terminal running while executing discovery or replay.

The application uses a fixed demo operator login:

```text
username: operator
password: demo123
```

Authentication is handled automatically by `agent/auth.py` before the
capability steps execute.

---

# Demo

Use **Terminal 2** for the following commands while the target application is
running in Terminal 1.

## 1. LLM-driven discovery

Load the Gemini key:

```powershell
conda activate interface-ai
$env:GEMINI_API_KEY="YOUR_KEY_HERE"
```

Run:

```powershell
python cli.py discover --flow member_lookup --goal "Look up member 12345 and read their savings and checking balance, then confirm you can see the member record" --entry-url http://127.0.0.1:5055/member/12345 --param member_id=12345 --headed
```

During discovery:

1. Playwright opens the real application.
2. The observer exposes actionable controls and readable page content.
3. Gemini receives the structured observation.
4. Gemini chooses the next action.
5. The automation executes that action against the live page.
6. The process repeats until Gemini declares the goal complete.
7. The successful transcript is compiled into a reusable artifact.

The resulting artifact is:

```text
artifacts/meridian.member_lookup_v1.json
```

Replay does not depend on Gemini after this artifact has been generated.

---

## 2. Successful deterministic replay

Run:

```powershell
python cli.py replay --artifact artifacts/meridian.member_lookup_v1.json --param member_id=12345 --headed
```

This executes the saved artifact without calling an LLM.

The submitted successful replay returned:

```json
{
  "status": "success",
  "outputs": {
    "savings_balance": "$4820.55",
    "checking_balance": "$1210.10"
  }
}
```

---

## 3. Business-outcome replay

Run the same capability with a nonexistent member:

```powershell
python cli.py replay --artifact artifacts/meridian.member_lookup_v1.json --param member_id=00000 --headed
```

Instead of treating an expected application result as an automation crash, the
replay engine returns a typed business outcome:

```json
{
  "status": "business_outcome",
  "business_outcome_code": "MEMBER_NOT_FOUND",
  "business_outcome_message": "No member found matching the given ID."
}
```

This demonstrates the distinction between:

- successful execution,
- expected business outcomes, and
- genuine automation failures.

---

## 4. List generated artifacts

```powershell
python cli.py list
```

---

# Artifact design

Discovery produces a structured, versioned capability contract.

The submitted member-lookup artifact is:

```text
artifacts/meridian.member_lookup_v1.json
```

It includes:

- typed input parameters,
- a parameterized entry URL,
- ordered execution steps,
- locator strategies,
- extracted outputs,
- known business outcomes,
- recoverable conditions,
- risk metadata, and
- a final success checkpoint.

For example, the discovered URL is generalized from the concrete member ID:

```text
http://127.0.0.1:5055/member/12345
```

into:

```text
http://127.0.0.1:5055/member/{{member_id}}
```

This allows the same artifact to be invoked with different member IDs.

---

# Deterministic replay

The replay engine does not import or call the LLM client.

It follows the generated artifact mechanically:

```text
Artifact
   |
   v
Resolve locator
   |
   v
Execute recorded action
   |
   v
Check known outcomes / recoverable conditions
   |
   v
Extract outputs
   |
   v
Validate checkpoint
```

Replay returns one of three top-level result categories:

### `success`

The workflow reached its declared checkpoint and produced its outputs.

### `business_outcome`

The target application returned an expected domain result such as
`MEMBER_NOT_FOUND`.

### `failure`

The automation encountered an unexpected condition such as an unresolved
locator, missing checkpoint, or unhandled execution error.

---

# Safety and guardrails

The automation includes several safety mechanisms.

## Domain allowlist

Automation is restricted to configured target domains through:

```text
config/allowlist.yaml
```

## Risk classification

Artifact steps carry a risk level so potentially irreversible actions can
require confirmation rather than executing silently.

## Redaction

Evidence passes through redaction logic before being written to disk.

## LLM isolation

The LLM is used for discovery only. Production replay does not silently fall
back to an LLM when deterministic execution fails.

---

# Human escalation

The project includes a human-intervention mechanism for situations where
automation should not continue independently.

Examples include:

- discovery declaring itself stuck, or
- replay reaching an action requiring human confirmation.

Escalation is implemented through `agent/escalation.py`.

A session records:

```text
control_owner = automation | human
```

When escalation occurs, automation stops interacting with the page while
waiting for intervention.

The operator console is implemented in:

```text
operator_console/console.py
```

It can attach to the same browser through Chrome DevTools Protocol (CDP), so
handoff is designed around the existing live browser context rather than
starting the workflow again in a new session.

See `REPORT.md` for the detailed handoff design and trade-offs.

---

# Tests

Run:

```powershell
python -m pytest tests -v
```

The tests cover core behavior including:

- artifact schema validation,
- parameter templating,
- locator generation,
- readable legacy-page observation,
- guardrails,
- redaction,
- business-outcome handling,
- recoverable retry behavior, and
- replay failure/control-flow behavior.

The final submitted version passed **32 tests**.

The test suite does not require a Gemini API key.

---

# Submitted evidence

This repository includes evidence from real end-to-end execution on a local
development machine.

## Successful LLM-driven discovery

```text
evidence/discovery_a10150758988/
```

This is a genuine Gemini-driven discovery against the live Meridian servicing
application.

The structured log records the model-driven sequence:

```text
observe
  -> extract savings_balance
  -> observe
  -> extract checking_balance
  -> observe
  -> assert member record
  -> observe
  -> done
```

The run completed with:

```text
success: true
artifact_id: meridian.member_lookup
```

The discovery evidence also contains a screenshot of the final browser state.

---

## Generated capability

```text
artifacts/meridian.member_lookup_v1.json
```

The successful discovery was compiled into this reusable capability artifact.

It defines:

```text
input:
  member_id

outputs:
  savings_balance
  checking_balance
```

and parameterizes the member-specific route using `{{member_id}}`.

---

## Successful deterministic replay

```text
evidence/replay_d097b1aa66eb/
```

This replay ran the generated artifact with **no LLM in the replay loop**.

It returned:

```text
status: success
savings_balance: $4820.55
checking_balance: $1210.10
```

The replay evidence includes its structured log and checkpoint screenshot.

---

## Business-outcome replay

```text
evidence/replay_95298ebb4fa4/
```

This replay invoked the same artifact with a nonexistent member ID.

It returned:

```text
status: business_outcome
business_outcome_code: MEMBER_NOT_FOUND
```

rather than incorrectly reporting the expected application response as an
automation failure.

---

# Evidence layout

```text
evidence/
├── discovery_a10150758988/
│   ├── meta.json
│   ├── run.jsonl
│   └── screenshots/
│       └── step04_done.png
│
├── replay_d097b1aa66eb/
│   ├── meta.json
│   ├── run.jsonl
│   └── screenshots/
│       └── checkpoint.png
│
└── replay_95298ebb4fa4/
    ├── meta.json
    └── run.jsonl
```

No Gemini API key or other external API credential is stored in the submitted
evidence or Git repository.

---

# Design report

For architecture, artifact-schema reasoning, deterministic replay,
heterogeneity/multi-tenant design, escalation, safety, and explicit trade-offs,
see:

[`REPORT.md`](REPORT.md)