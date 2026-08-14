# REPORT

## 1. Architecture

Single Python process, two entry points (`discover`, `replay`), sharing one
core library (`agent/`). No queues, no services, no multi-tenant plumbing —
the brief is explicit that building that infrastructure prematurely isn't
rewarded, so I kept the architecture as flat as the problem allows and
focused effort on the abstractions that would need to survive scaling up.

```
        discover                                  replay
           |                                         |
   agent_loop.py (LLM in loop)          replay.py (LLM NEVER in loop)
     - dom_observer.observe()             - locator_resolver.resolve()
     - llm_client.decide()                - error taxonomy (KnownOutcome /
     - locator_resolver.resolve()           RecoverableCondition / hard fail)
     - guardrails checks                  - guardrails checks
     - escalation on "stuck"              - escalation on risky/unconfirmed
           |                                         |
           +------------> schemas.Artifact <---------+
                         (the shared contract)
                                |
                         evidence.py (both sides log here)
```

**Key decisions:**

- **DOM/accessibility-grounded observation, not screenshot+coordinates.**
  The brief explicitly biases toward approaches that survive a UI with no
  clean DOM. Coordinates are quick to get an agent working with, but they
  break the moment layout shifts even slightly, and they give the LLM
  nothing to reason about semantically. I extract both actionable controls
  and bounded read-only page content (including legacy table cells) with
  short refs and generated stable-ish locators (name attr, nearest
  label-cell text, visible text, etc.) and hand that to the model as text.
  This lets discovery read values such as balances without pretending they
  are buttons or inputs. This is the same representation the artifact stores, so there's
  no lossy translation between what the model saw and what gets replayed.

- **Gemini function-calling behind a narrow LLM seam.** Discovery uses
  Gemini 3.5 Flash-Lite through Google's official Gen AI SDK and asks for exactly one
  schema-backed `agent_action` function call per observation. The SDK is configured
  with function-calling mode `ANY`, automatic execution disabled, and the returned
  arguments are validated before Playwright sees them. Replay never imports the
  LLM client at all.

- **One process, sync Playwright.** The interesting design problems here
  (artifact schema, error taxonomy, control-transfer model) don't need
  async/distributed infrastructure to demonstrate, and a sync loop is much
  easier to read and defend line-by-line, which matters given the
  evaluation criteria.

- **Discovery and replay never share code paths for *deciding* what to
  do** — only for *acting* (`locator_resolver.py`) and *safety*
  (`guardrails.py`). This is deliberate: it's the clearest way to guarantee
  replay genuinely has no model in the loop, rather than trusting a flag
  somewhere isn't accidentally left on.

**Trade-off I'm aware of:** discovery and replay each launch their own
browser rather than sharing a session pool. That's the right simplicity
for this scope, but a production version would want a session-manager
service that discovery, replay, *and* the operator console all attach to,
so a stuck replay could hand off without paying for a second browser boot.

## 2. Artifact schema

The artifact (`agent/schemas.py::Artifact`) is designed to answer three
questions for three different readers at once:

- **A human reviewer:** "what does this do, and is it safe to approve?" —
  `description`, `goal_template`, each step's `notes` and `risk_level`.
- **A calling AI agent:** "what do I pass in, what do I get back?" —
  `input_params` (typed, required/optional, `sensitive` flag) and
  `outputs` (typed, tied to the step that produced them).
- **The replay engine:** "how do I actually do this, robustly?" — `steps`
  (ordered `ActionType` + `ElementTarget`), `checkpoint`, and per-step
  `known_outcomes` / `recoverable_conditions`.

Locators are never a single string. `ElementTarget` is a `primary` locator
plus an ordered `fallbacks` list, each with its own `LocatorStrategy`
ranked by expected stability (`test_id` > `role_name` > `form_field_name` >
`label_text` > `exact_text` > `css`). `locator_resolver.py` walks this list
at replay time and records which strategy actually resolved — that's a
direct, cheap signal for the "confidence scoring" stretch goal (an artifact
that's constantly falling through to its 3rd fallback is telling you
something about drift, without needing a separate scoring subsystem).

Parameterization happens by value-matching during discovery
(`agent_loop.py::_templatize`): if a value the model typed matches one of
the input parameters supplied for that run, it's stored as `{{param_name}}`
rather than the literal. This is simple and deterministic, and it means the
artifact is reusable across different member IDs/amounts immediately,
without the LLM needing to explicitly annotate what's a "variable."

`status: draft | approved | deprecated` exists so replay can optionally
gate unattended execution on human review (`--require-approved`), which
is how "reviewable" becomes an actual runtime constraint and not just
documentation.

## 3. Determinism & error handling

Replay never calls the model. Every step is a pre-recorded `(action,
ElementTarget, templated value)` tuple; the only "decision" replay makes is
mechanical: try locator strategies in order, then check page state against
declared matchers.

The result contract (`ReplayResult`) is a **closed set of three
outcomes**, and the engine is structured so a step can only produce one of
them, never something ambiguous:

- **`SUCCESS`** — the declared `checkpoint` resolved, and any `outputs`
  were extracted. Nothing about "probably worked."
- **`BUSINESS_OUTCOME`** — a `KnownOutcome` matcher on the current step
  fired (e.g. "No member found matching ID", "Access Denied"). This is a
  first-class result with a `code` and message, returned immediately —
  never wrapped as an exception, never conflated with a bug.
- **`FAILURE`** — anything the artifact didn't anticipate: a locator that
  never resolves after all fallbacks, a checkpoint that never appears, an
  unhandled exception. Carries `step_id`, `step_index`, `expected` vs.
  `observed`, and a screenshot + DOM snapshot path.

Recoverable conditions (`RecoverableCondition`) sit between those two: a
matcher (e.g. "System Temporarily Unavailable") plus a strategy
(`dismiss_and_retry` / `wait_and_reload`) and a bounded `max_retries` +
`backoff_ms`. The target app simulates exactly this (member `55555` fails
once, then succeeds), and it's covered by an integration-style test that
mocks the page and asserts the engine retries once and then proceeds
(`tests/test_replay_flow.py`). If retries are exhausted, it falls through
to `FAILURE` rather than looping forever.

Where do the `known_outcomes`/`recoverable_conditions` matchers come from?
Not purely from the LLM's single happy-path discovery run — a run that
successfully finds member 12345 doesn't automatically know what "not
found" looks like. I attach a small per-flow **domain-knowledge library**
(`agent_loop.py::KNOWN_OUTCOME_LIBRARY`) during artifact compilation,
representing what a human reviewer/author would encode about that vendor
app's known error surfaces. This is an honest design choice, not a
simplification I'm hiding: in production this would be a growing library
per vendor product, populated from prior recordings, incident reports, and
review — the discovery run's job is to find *a* successful path once; the
error taxonomy is a reviewed, versioned artifact property, same as the
happy-path steps are.

Native dialogs (`confirm()`/`alert()`) are handled explicitly: a step can
declare `expect_dialog=True`, and the harness installs a one-shot dialog
handler before acting so an *expected* confirmation is accepted
deterministically, while any *unexpected* dialog is auto-dismissed and
logged loudly rather than silently blocking the run forever.

UI drift (secondary concern per the brief, since this environment is
stable-but-erroring rather than fast-changing) is handled by the same
fallback-locator mechanism: a class name changing or a column being added
to a table degrades gracefully through the fallback chain instead of
hard-failing on the first strategy.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `locator_resolver.py`:
`ElementTarget`/`Locator` are surface-agnostic *descriptions* ("form field
named X", "role=button name='Y'"), and only `locator_resolver.resolve()`
knows how to turn one into a live action. Extending to a legacy web app
with framesets is mostly already handled — `ElementTarget.frame_path`
exists for exactly this, and `_frame_for()` walks nested `frame_locator()`
calls. Extending to a native desktop app means adding a new
`LocatorStrategy` (e.g. `accessibility_id` targeting an OS accessibility
tree via a framework like pywinauto/UIA or macOS Accessibility API) and a
new branch in `_locate()` — the `Artifact`/`Step`/`ElementTarget` schema
doesn't change at all, because it was never Playwright-specific in the
first place (no CSS-only assumptions baked into the pydantic models). The
`ActionType` enum (click/fill/select/extract/assert) maps naturally onto
desktop widget verbs too.

**Multi-tenant reuse.** `TargetScope.tenant_id` is `None` for a "base"
recording — a capability recorded once against a vendor product's default
configuration, not against any one institution. The concrete reuse story I
did **not** build (per the brief's "design, not necessarily build" for
this section) but would implement next:

- **Canonicalize concrete values into route/label patterns** at
  compile-time (`/member/12345` → `/member/{{member_id}}`, already
  partially done via the value-templating in section 2) so the same
  artifact works across tenants whose *data* differs but whose *screens*
  don't.
- **A per-tenant override layer**, not a per-tenant re-record: a small
  overrides document keyed by `(base_artifact_id, tenant_id)` that can
  patch specific `Locator`s or `known_outcome` matchers (e.g. tenant B's
  white-labeled instance renamed a button, or moved a banner's CSS class).
  Replay would resolve `base + override` at load time. This keeps the
  "many tenants run the same vendor product" case as *N small diffs* over
  *one* artifact, not *N* independent artifacts.
- **Drift detection**: the locator-resolver already records which fallback
  strategy actually fired per run (section 2). Aggregating that across
  replays per `(artifact_id, tenant_id)` gives a cheap flakiness/drift
  signal — an artifact whose primary locator stops resolving for one
  tenant but not others is very likely a tenant-specific override
  candidate, not a global re-record.

## 5. Escalation & handoff

Two situations trigger escalation, both implemented in the system:

1. **Discovery is stuck** — the model calls the `agent_action` tool with
   `action="stuck"` (it's instructed to do this rather than guess when it
   hits something it can't safely resolve, e.g. an access-denied page).
2. **Replay hits a risky step that isn't pre-confirmed** — governed by the
   `risky_action_policy` in `config/allowlist.yaml` (`block` /
   `require_confirmation` / `allow_with_flag`).

Both funnel into the same mechanism (`agent/escalation.py`): a small
file-backed state machine per run (`sessions/<session_id>.json`) with a
`control_owner` (`automation` | `human`) and a `status`. When automation
escalates, it writes an `intervention_request` (reason, step context,
screenshot path) and blocks on `wait_for_resume()` — it does **not** keep
touching the page while waiting, so there's never a race between the
automation process and a human for control of the same session.

The handoff is real in the sense that matters: the browser is launched
with `--remote-debugging-port`, and the operator console
(`operator_console/console.py`) is a genuinely separate OS process that
attaches via `chromium.connect_over_cdp(cdp_endpoint)` to the **same
browser, same page** — not a fresh tab, not a screenshot replica. A human
(here, a CLI operator) can `look` (re-observe), `click`/`fill`/`select` by
element ref, and every action is recorded to the session file
(`human_actions`) before `resume` flips `control_owner` back to
`automation`, unblocking the waiting run, which then continues from
wherever it left off — same cookies, same page, same in-progress form.

What I mocked, deliberately: the operator UI is a CLI, not a real-time
co-browsing view (out of scope per the brief). What's *not* mocked: the
CDP attach, the control-owner state machine, the blocking/polling contract,
and the evidence trail of exactly what the human did.

## 6. Safety

Two independent layers, checked before every action in both discovery and
replay:

- **Allowlist** (`config/allowlist.yaml` → `Guardrails.check_domain` /
  `check_action_type`): a hard boundary on *where* (domain glob patterns)
  and *what kind of action* is permitted at all. A `GuardrailViolation`
  always stops the run — it's never downgraded to a warning.
- **Risk policy** (`Guardrails.requires_confirmation`): every step carries
  a `RiskLevel` (`safe` / `reversible` / `risky`). I chose to have the
  *artifact author* mark specific steps `risky` explicitly (e.g. the
  confirm()-guarded "Open Sub-Account" submit) rather than trying to infer
  irreversibility purely from the action type — a `click` is safe on a
  "Search" button and risky on a "Submit Transfer" button, and only
  someone who understands the target app's semantics can make that call
  correctly. The *policy* for what to do about a risky step is
  configurable and conservative by default (`require_confirmation`): it
  runs only with an explicit `confirm_risky` invocation argument or after
  a human hands control back via the escalation path above. `block` is
  available for anything that should never run unattended regardless of
  confirmation.
- **Redaction** (`guardrails.py::redact_text` / `redact_dict`): applied to
  every evidence log line and DOM snapshot before it's written — both
  pattern-based (SSN-shaped numbers, account-number-shaped digit runs,
  anything that looks like a credential) and schema-driven (any
  `InputParam`/`OutputField` marked `sensitive=True` is redacted by name,
  not left to a regex to catch).

**Limits, honestly stated:** the pattern-based redaction is a safety net,
not a guarantee — a regex can't catch every shape of PII, and a production
system handling real member data would need field-level redaction
declared for *every* sensitive value the target app can display, not just
the ones I thought to pattern-match. The allowlist here is a single flat
policy; a real deployment needs it scoped per tenant/app, which the schema
supports (`TargetScope.allowed_domains`) but I didn't build a config layer
for.

## 7. Cuts

What I deliberately left thin, stubbed, or out — and why:

- **Multi-tenant reuse and desktop-surface support are designed, not
  built** (see section 4), exactly as the brief scopes it.
- **The operator console is a CLI, not a GUI**, per the brief's explicit
  scope note. The control-transfer mechanism it exercises is real.
- **Login/session establishment is infrastructure, not a recorded step.**
  `agent/auth.py` authenticates a fresh browser context once before the
  recorded/replayed steps run, rather than making "how to log in" part of
  every artifact. This is a real design choice, not a shortcut: it mirrors
  how a real deployment would sit behind SSO or a service-account token
  that's established once per session, independent of which capability is
  being invoked. Driving the login form itself *as a recorded step* would
  be mechanically identical to every other step here (`fill` + `fill` +
  `click`), so it wasn't a differentiated problem worth spending discovery
  runs on.
- **Confidence scoring and cross-tenant override application (stretch
  goals) are not implemented**, though the locator-resolver's
  strategy-used logging is the exact hook a confidence score would consume
  — see section 2.
- **No LLM-assisted replay fallback** (a stretch goal) — I'd rather ship a
  smaller number of genuinely deterministic capabilities than a system
  that quietly falls back to guessing when a locator doesn't resolve.
- **Single target surface, one vendor app** — as scoped. I chose a
  deliberately unfriendly one (no test IDs, table layout, native dialog)
  rather than a clean modern site, since that's the actual hard case this
  system exists for.

**What I'd build next with more time:** the per-tenant override layer
described in section 4 (highest leverage for the real environment), a
proper session-manager so discovery/replay/operator share a browser pool
instead of each launching its own, and multi-run stability reporting
(replay N times, aggregate which locator strategy resolved each run) as
the natural next step after the confidence-signal groundwork already in
`locator_resolver.py`.
