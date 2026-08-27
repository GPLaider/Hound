# State machine

```text
created → blocked (verification proposal; no preflight or worker)
created → baseline → working
working → verifying → reviewing → done
working → scouting → culling → synthesizing → working
working → blocked | budget_exhausted | cancelled | failed_internal
reviewing → working (revise)
```

The engine—not worker JSON—sets state. A marker-only verification proposal is persisted and moves directly from `created` to `blocked`; it runs no capability preflight, worker, or proposed command. `baseline` atomically records the initial Git HEAD and pre-existing dirty-path identity. A Writer returns `continue`, `candidate_done`, or `blocked`. Machine and baseline-relative path gates follow each Writer. `candidate_done` enters read-only review only after they pass. The first attempt uses `rounds/<n>/verifier/`; resumed attempts use `rounds/<n>/review-resume-<k>/verifier/`. `final/verification.json` and `final/verifier.json` snapshot the latest proof before terminal state. A rejection becomes Writer context without overwriting prior attempts. Repeated identical rejection can trigger Pack; if that Pack adds no evidence, Hound stops `blocked` before another review cycle.

Resume holds the workspace lock and validates every launch without an outcome before changing durable state. A still-live worker, malformed live identity, or unverifiable live Windows identity raises a retryable environment error with PID/PPID/executable evidence; the CLI exits `4`, state and summary remain unchanged, and Hound neither adopts nor kills the orphan. Dead launches receive `interrupted` records. A completed Writer is reconstructed only from `writer-intent.json` plus `result.json` or `final.txt`; partial streams alone stay interrupted evidence. Redaction, hashing, and AgentFlow completed-node recovery run asynchronously, and recovered run state commits only after the batch succeeds. `active_started_at` charges the uncertain in-flight interval to the cumulative wall budget. An unchanged failed action fingerprint is refused before another Writer and may trigger a read-only Pack.

Summary and terminal events are durable before terminal state is committed. If either write fails, persisted state remains non-terminal. Unexpected engine exceptions become redacted `failed_internal`. Configuration, preflight, lock, and retryable-resume failures exit `4`; an ordinary environment failure after a run has started becomes durable `blocked` and exits `3`.

Round and worker counts, active execution seconds, and the in-flight start timestamp are durable. Resume therefore cannot reset or undercount any configured budget.
