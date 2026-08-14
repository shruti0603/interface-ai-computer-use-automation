# /evidence

This directory is populated by running `python cli.py discover ...` and
`python cli.py replay ...` (see the root README's "Demo path"). Each run
creates its own subdirectory:

```
discovery_<run_id>/
  run.jsonl        one JSON event per observation/decision/action, in order
  screenshots/      PNGs at key moments (goal reached, stuck, hard failure)
  meta.json         run summary (success, step count, artifact produced)

replay_<run_id>/
  run.jsonl        step_ok / business_outcome_detected / recoverable_condition_detected /
                   hard_failure events, in order
  screenshots/      PNGs on checkpoint success and on any failure
  meta.json         run summary (status, business outcome code if any, failure detail if any)
```

Per the assignment brief, this repo does not fabricate evidence for a run
that didn't actually happen. The repository intentionally does not include fabricated run evidence. Before
submission, run the demo commands on a machine with Chromium installed and a
real Gemini `GEMINI_API_KEY`; commit the resulting discovery/replay evidence
directories after checking that no sensitive values were captured.

For a complete demonstration, run:
1. One discovery run for `member_lookup` (happy path).
2. One discovery run for `open_subaccount` (multi-field form + native
   `confirm()` dialog + a risky step).
3. One replay of `member_lookup` with a valid member ID (SUCCESS).
4. One replay of `member_lookup` with an invalid member ID (BUSINESS_OUTCOME
   - "no such member" is a legitimate result, not a crash).
5. One replay of `open_subaccount` without `--confirm-risky`, showing the
   escalation pause, then use `operator_console/console.py` to resume it.

That set exercises every branch of the result contract
(`SUCCESS` / `BUSINESS_OUTCOME` / `FAILURE`) plus the human-escalation path,
which is what the brief specifically asks evidence to show.
