# Adaptive Pack

Pack triggers when explicitly on, requested with distinct missions, or justified by a blocked Writer, repeated identical machine/Verifier failure, or idle evidence. It is skipped for clear solo work and insufficient budgets. After repeated final-review failure, an evidence-empty Pack ends the run `BLOCKED` instead of enabling another empty review cycle.

`backend = "auto"` is AgentFlow-first for a qualified Pack. Hound delegates a small AgentFlow JSON pipeline whenever at least two distinct non-empty missions and two workers fit concurrency and remaining budget and the enabled CLI is available. Missions may come from the Writer or Hound's bounded blocked/idle/repeated-failure defaults. Otherwise Hound uses its direct Pack. Every automatic choice is persisted as `pack_backend_selected`.

When qualified, AgentFlow supplies bounded parallel execution and its own durable node artifacts. Each generated read-only node permits one retry; Hound's whole-Pack timeout and total-worker budget remain the outer bounds. Hound imports and redacts those artifacts, scores completed/recovered results after the run, retains the strongest configured evidence, and gives a byte-bounded digest of useful completed/partial findings to the next sole Writer. On Hound resume, completed native AgentFlow nodes are imported during the asynchronous recovery batch; recovered run state advances only after validation, redaction, import, and hashing succeed. Useful Pack evidence resets the idle counter; failed, empty, or duplicate-only results do not postpone the configured idle stop.

Hound deliberately does not import AgentFlow as a Python dependency or expose its DAG. If AgentFlow is absent or fails before completing any Scout, `auto` falls back to Hound's direct Pack.

The direct backend keeps the live culling policy. It always waits `minimum_runtime_seconds`. Until the observation deadline, it also waits while any live Scout has fewer than `minimum_progress_events`; a genuinely stalled Scout cannot defer the Pack forever. Deterministic scoring uses concrete evidence, relevant terms/paths, hypotheses, progress events, repetition, errors, and output efficiency. After `judge_interval_seconds`, if the bottom scores remain closer than `minimum_score_gap`, concurrency and worker budget allow one read-only Judge to contribute semantic scores:

```text
final = deterministic * 0.6 + semantic * 0.4
```

Only one live Scout is culled per cooldown. The supervisor refuses to cull below `survivors` and protects a worker carrying unique path evidence when an alternative exists. Every cull writes timestamp, score, order, and reason; stdout/stderr/trace/final partials remain available to synthesis. Both backends write `pack-ranking.json` with scores, statuses, retention, and survivor IDs. `synthesis.json` carries the baseline-relative diff, compact Scout evidence, cull reasons, deduplicated hypotheses, and recommended next actions; only a bounded digest enters later prompts.

AgentFlow CLI cancellation is whole-run only, so its ranking is honest post-run ranking rather than live per-node culling. On Windows, Hound never asks AgentFlow to kill a single node: Hound's outer timeout is shorter than node timeouts and can terminate only the guarded direct AgentFlow PID tree with system `taskkill.exe /PID <pid> /T /F`. The target identity and complete subtree must pass the same protected-process guard as direct workers; console-control events are never used. Completed nodes are recovered from persisted native `run.json`; unfinished nodes become `interrupted`.
