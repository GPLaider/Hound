# Decisions

1. Implement in an empty repository root.
2. Runtime dependencies remain zero; `pytest` is test-only.
3. Use a fixed state machine, not a public DAG DSL.
4. Default to one Writer. Pack is conditional and capped by configured concurrency.
5. Keep one OS-backed execution lock per canonical workspace outside the Writer sandbox; this replaces redundant Writer and per-run locks.
6. Use argv-only child execution. `--verify-argv` is the unambiguous verification interface; `--verify` uses the native platform argument parser.
7. On Windows, bind each direct worker PID to its kernel creation time and image path, validate the full Toolhelp subtree before exact PID-tree `taskkill`, and refuse Hound, ancestors, protected shells/apps, PID reuse, and non-child targets. Never use console-control events.
8. Use deterministic live scoring first. Invoke one semantic Judge only when deterministic separation is insufficient.
9. Copy no reference source. Preserve commit and license evidence in `NOTICE.md` and `docs/reference-audit.md`.
10. Do not auto-install the skill or modify global Codex configuration.
11. Hound owns persistence, the sole Writer, and completion. A qualified Pack `auto` prefers AgentFlow when native member culling is safe or unnecessary; otherwise it selects guarded direct supervision. No AgentFlow Python dependency or public DAG is added.
12. AgentFlow mode uses one native Scout fanout and, when platform, policy, and budget allow, a read-only periodic controller. Hound audits its native action envelope and applied-action log; only one confirmed member cancellation per compliant tick is accepted. Windows omits that controller because AgentFlow's local runner bypasses Hound's guarded PID-tree termination; guarded direct mode owns Windows live culling.
13. Production Codex prompts travel over stdin, complete process output stays in artifacts, and only bounded tails stay in supervisor memory.
14. Resolve child executables explicitly without Windows' implicit current-directory lookup; use ephemeral Codex workers because Hound owns persistence, and isolate their `CODEX_HOME` so Codex cannot persist project trust into the user's global config. Snapshot non-system user skills into that home so required safety skills remain available without loading global config.
15. Persist active execution seconds so resume cannot reset the wall budget.
16. Treat terminal `state.json` as the final completion commit; summary/event failures leave the run non-terminal, while unexpected engine defects become redacted durable `failed_internal` state.
17. Redact inherited secret values in the common streaming/process cleanup path and carry only a bounded, one-round recovery digest into a resumed Writer.
18. Capture one immutable run baseline containing Git HEAD plus staged/worktree identity for pre-existing dirty paths. Allowed/forbidden path gates compare against this baseline and fail closed when a requested comparison is unavailable.
19. Persist `active_started_at` before execution. A crash leaves it set, so resume conservatively charges the uncertain interval instead of resetting the wall budget.
20. Refuse a scheduled Writer action before launch when the same fingerprint already failed with unchanged evidence; use a Pack for a different strategy when budget and policy permit.
21. Direct culling uses minimum runtime, bounded progress observation, Judge interval, one-at-a-time cooldown, unique-evidence protection, and a durable ranking/synthesis digest shared with the next Writer.
22. Store the first Verifier attempt under `rounds/<n>/verifier/` and resumed attempts under `rounds/<n>/review-resume-<k>/verifier/`; separately snapshot the latest completion evidence under `final/` before terminal state.
23. `hound doctor` honors configured executables, performs a real temporary workspace write, and reports the OS lock snapshot. Approved run/resume contracts require sandbox, `approval=never`, user-config isolation, isolated `CODEX_HOME`, and trusted Python while omitting unsupported optional output flags; proposal-only contracts do not preflight.
24. AgentFlow Scouts receive one bounded retry and the controller receives none. Persist the stable Hound worker/expanded-node map, charge the controller to the worker budget, and import native member cancellations as durable culled evidence without reruns or inner-PID handling.
25. Keep the durable loop and completion authority in `engine.py`, but place read-only Pack supervision in `pack.py`, evidence identity in `evidence.py`, final review in `review.py`, and crash reconstruction in `recovery.py`. The 500-line module target is soft; `engine.py` and `process.py` are documented exceptions, not a false invariant.
26. Auto-approve only `compileall` for Python under `src/` and `py_compile` for root Python files. Package-marker commands are durable proposals that stop `BLOCKED` without preflight or execution until supplied explicitly through `--verify-argv`.
27. Validate every unfinished launch before mutating resume state. A still-live, malformed live, or unverifiable live identity is retryable exit `4`; Hound neither adopts nor kills it and does not change state or summary.
28. Restore a Writer action only from its durable intent plus completed final/result artifact. Partial output remains interrupted evidence and must not silently become a completed action.
29. Perform resume redaction, native AgentFlow completed-node import, and hashing asynchronously; commit recovered run state only after the batch succeeds.
30. A blocked Writer and repeated identical Verifier failure may trigger Pack. If repeated final review is followed by a Pack with no new evidence, stop `BLOCKED` instead of cycling again.
31. Pin every worker PATH to the Python running Hound, remove Python launcher overrides, and forbid searching application directories or executing embedded runtimes.
32. On native Windows, explicitly select the `elevated` Codex sandbox per invocation. Writer remains `workspace-write`; every other Codex role remains `read-only`; no worker may request escalation.
33. Start Windows children with no console window and process-error dialogs suppressed. A termination guard refusal is durable evidence and never falls through to another kill mechanism.
34. Copy authentication and non-system user skills only for real Codex/AgentFlow launches; exclude `config.toml`, `.system`, caches, and bytecode. Delete isolated homes on normal CLI exit; after a hard crash, reap only Hound-marked direct Temp children whose filename owner PID is no longer live.
35. Validate imported AgentFlow final responses with the same closed Scout schema as direct Codex workers. A completed native node carrying only JSONL progress is failed evidence, never a completed Scout result.
36. Treat `candidate_done` as a request for Hound's authoritative machine and final-review gates. Writers must not duplicate contract verification or remain `continue` only because worker-local Python is unavailable.
