# Hound

Hound keeps one Codex Writer working until machine checks and an independent Verifier accept the result. When uncertainty, repeated failure, or explicit Pack policy justifies extra investigation, Hound runs bounded read-only Scouts and returns their evidence to the single Writer. A qualified `auto` Pack prefers installed AgentFlow for parallel hunts and keeps the direct supervisor as its zero-dependency fallback.

## Install

Requires Python 3.11+ and an authenticated `codex` CLI. AgentFlow is optional and used only for a qualified parallel Pack or an explicit backend override.

```text
python -m pip install -e .
hound doctor
```

Runtime dependencies: none. AgentFlow is invoked as an external CLI and is not installed or modified by Hound.

`hound doctor` honors the configured Codex and AgentFlow executable names, performs a real create/write/fsync/delete workspace probe, reports the non-mutating workspace-lock snapshot, and inspects CLI capabilities. Before an approved execution contract starts workers, `run` or `resume` separately negotiates the installed `codex exec` flags. Role sandboxing, non-interactive `approval=never`, user-config isolation, and the trusted Hound Python are mandatory; optional output flags are negotiated. Proposal-only runs never reach this preflight.

## Quick start

```text
hound init
hound run "fix the failing login test" --verify "python -m pytest -q"
hound status
hound inspect <run-id>
hound resume <run-id>
```

`--verify` is parsed with the native platform argument parser and is never passed to a shell. `--verify-argv` accepts a JSON string array when exact argv is preferable; quote that JSON for the invoking shell. Repeat either option for multiple gates.

If verification is omitted, Hound auto-approves only safe standard-library compilation: Python below `src/` uses the current interpreter with `-m compileall -q src`; otherwise root `*.py` files use `-m py_compile <sorted-files>`. A marker-only root does not authorize package scripts or tests. Instead, `pyproject.toml`, `package.json`, `Cargo.toml`, and `go.mod` produce proposal commands for `pytest -q`, `npm test`, `cargo test`, and `go test ./...`. Hound persists those arrays as `verification_proposals` in `contract.json`, creates a durable `BLOCKED` run with exit `3`, starts no preflight, worker, or verification command, and prints the exact JSON arguments to copy into `--verify-argv`. Resuming that run remains blocked; starting a new run with those explicit arrays approves execution.

## Execution

The fixed workflow is baseline → solo Writer → machine verification → final read-only Verifier. Baseline captures the initial Git HEAD plus the identity of every already-dirty path, including index entries and final file content. Path gates therefore evaluate committed and worktree changes relative to the run start instead of treating pre-existing dirt as Hound's work; when a requested path comparison cannot be established, it fails closed. Failed verification and the current baseline-relative diff become next-round evidence. Pack scouting is considered for `--pack on`, a structured parallel request, a blocked Writer, repeated identical machine or Verifier failures, or idle rounds. `--pack off` guarantees the solo path.

Pack backend defaults to AgentFlow-first `auto`. Whenever a Pack has at least two distinct missions, at least two workers fit concurrency and budget, and the enabled AgentFlow CLI is available, Hound delegates the bounded read-only hunt. This includes default missions derived from blocked, idle, or repeated-failure evidence. Missing or failed AgentFlow falls back to direct supervision. Override with `--pack-backend agentflow` or `--pack-backend direct`.

Each round returns `continue`, `candidate_done`, or `blocked`; only Hound can set the durable run status. A scheduled action whose fingerprint already failed with the same evidence is refused before another Writer starts; Hound seeks different Scout evidence or stops honestly. `candidate_done` is not completion until machine gates, baseline-relative path gates, and final review pass. The first Verifier attempt uses `rounds/<n>/verifier/`; attempts made while resuming the same round use `rounds/<n>/review-resume-<k>/verifier/`. `final/verification.json` and `final/verifier.json` snapshot the latest completion evidence before terminal `state.json` becomes the commit marker. After the same final-review failure repeats, Hound runs a Pack when policy and budget allow; if that Pack adds no evidence, the run stops `BLOCKED` instead of repeating another empty review cycle. Unexpected engine defects become durable `failed_internal` runs.

## State and resume

Runs live under `.hound/runs/<run-id>/`: atomic `state.json`, `contract.json`, `baseline.json`, fsynced append-only events/fingerprints/cull decisions, and per-worker stdout, stderr, JSON trace, final text, prompt hash, launch/outcome records, and parsed result. `active_started_at` is persisted before execution and folded into `active_seconds` on resume; after an orchestrator crash, uncertain elapsed time is conservatively charged to the wall budget instead of being reset. Recovery first validates unfinished launches. On Windows, a recorded creation time is compared only after rehydrating the live PID's kernel identity, so the original worker is not mistaken for a reused PID. A still-live worker, malformed live identity, or unverifiable live Windows identity raises a retryable environment error with PID/PPID/executable evidence; the CLI exits `4` without changing run state or summary and never adopts or kills the orphan. Dead launches become `interrupted`. A Writer round is reconstructed only from durable `writer-intent.json` plus a completed `result.json` or `final.txt`; partial stdout or traces alone never become a completed Writer action. Redaction, AgentFlow completed-node import, and artifact hashing run asynchronously, and recovered run state is committed only after the recovery batch succeeds. A byte-capped digest of partial artifacts, structured results, Pack ranking, and cull decisions is supplied to the immediately following Writer round only.

## Pack execution

Scouts are read-only, bounded by `concurrency`, and receive distinct missions. A qualified `auto` request writes a small AgentFlow JSON pipeline with one bounded retry per node, invokes `agentflow run`, imports AgentFlow node traces/results into Hound artifacts, ranks completed evidence, and marks the configured survivor set for synthesis. A native node counts as completed evidence only when its final response matches Hound's Scout schema; JSONL progress events cannot masquerade as a final result. If Hound's outer timeout ends the AgentFlow process tree, or Hound itself later resumes after a crash, completed AgentFlow nodes are recovered from durable native `run.json` data and unfinished nodes become `interrupted`.

The direct fallback retains live score-based sequential culling. It invokes Codex Scouts directly, always waits the configured minimum runtime, and then waits for minimum progress only up to a bounded observation deadline. It may call one Judge after `judge_interval_seconds`, and culls one low scorer per cooldown while protecting unique evidence. Each Pack writes a durable ranking, survivor set, deduplicated synthesis, and a byte-bounded digest for the next Writer. Every automatic selection is recorded as `pack_backend_selected` in the run event log.

## Safety

- An OS-backed lock outside the Writer sandbox permits one Hound run per canonical workspace; different workspaces remain independent. The OS releases it on process exit, so stale PID-file reclamation is unnecessary.
- Scout, Judge, and Verifier use Codex `read-only`; Writer uses `workspace-write`.
- Workers are forbidden from nested agents and Hound invocation.
- AgentFlow receives only independent read-only Codex nodes; it never receives a Writer node from Hound.
- Child processes use argv arrays, not shell execution.
- Executables are resolved to explicit paths without Windows' implicit current-directory search, preventing a workspace file from shadowing Codex, AgentFlow, Git, or verification tools.
- Real Codex receives an ephemeral `CODEX_HOME` containing a temporary authentication copy and a snapshot of non-system user skills; fake/test prefixes receive neither. Normal exit removes it, and a later invocation reaps only marked Temp-directories whose recorded owner PID is dead. Global `config.toml` and `.system` skills are never copied, so worker trust/config/runtime writes cannot modify the user's Codex configuration; `--ignore-user-config` remains explicit.
- Windows workers explicitly select the stronger native `elevated` sandbox. Writer uses `workspace-write`; Scout, Judge, and Verifier use `read-only`; approval escalation is disabled with `approval=never`.
- Worker PATH starts with the exact Python running Hound, removes `PYTHONHOME`/launcher overrides, and forbids runtime scavenging through LibreOffice or other application installations.
- Production Codex prompts use stdin (`codex exec -`), so prompt contents do not enter the Windows command line. Other workers inherit no hosted stdin.
- Complete stdout/stderr stay on disk while in-memory tails are bounded.
- Windows workers use no console window and suppress inherited process-error dialogs. Cancellation binds the direct child PID to kernel creation time and image path, snapshots the entire subtree, rejects Hound/ancestors and protected processes such as Explorer or ChatGPT anywhere in that tree, then calls only system `taskkill.exe /PID <pid> /T /F`. Console-control events are never used.
- Secret-looking environment values are boundary-safely redacted from prompts, streamed logs, traces, final/results, and imported AgentFlow text artifacts, including timeout and cancellation cleanup.
- Hound never edits Codex `config.toml`, manages API keys, installs AgentFlow, or changes the AgentFlow installation.

## Skill

```text
hound install-skill
hound uninstall-skill
```

Installation is project-local at `.agents/skills/hounds/SKILL.md`, never automatic, and refuses overwrite/removal of unowned content without explicit `--force` on install.

## Exit codes

- `0`: DONE
- `2`: BUDGET_EXHAUSTED or verification did not complete
- `3`: BLOCKED
- `4`: configuration/environment error, including retryable live-orphan resume refusal
- `5`: internal error
- `130`: CANCELLED

## Current limits

- Codex is the only adapter.
- AgentFlow Pack mode has one bounded node retry and ranks imported results after node completion; individual live quality culling remains a `direct` backend feature because AgentFlow's CLI exposes whole-run cancellation, not safe per-node cancellation.
- Windows graceful console signaling is intentionally omitted; guarded PID-tree force termination is safer in hosted consoles.
- Hound is local-only: no DAG DSL, daemon, web/TUI, Docker, SSH, cloud runner, provider manager, or parallel code writers.
- Semantic judging is best-effort; deterministic scoring remains authoritative when Judge output is missing or invalid.

See [architecture](docs/architecture.md), [state machine](docs/state-machine.md), [Pack policy](docs/pack-culling.md), [reference audit](docs/reference-audit.md), and the [current completion audit](docs/completion-audit.md).
