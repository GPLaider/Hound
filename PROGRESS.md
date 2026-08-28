# Working path: C:\Users\Administrator\Documents\ChatGPT\hound

## Status

The 0.2.0 safety overhaul, real Codex and AgentFlow smokes, self-hosted recovery fixes, Lyriq skill projection, user-wide skill CLI alignment, and release-candidate audit are complete. The latest pass adds native AgentFlow fanout plus policy-audited member culling where platform-safe, while Windows preserves guarded direct-only live termination. Final isolated package checks did not mutate Codex global configuration or the installed AgentFlow.

## Current implementation

- Zero mandatory runtime dependencies; fixed local state machine rather than a public DAG.
- One workspace-write Writer. Scout, Judge, and Verifier remain read-only; nested Hound/agent fan-out is forbidden.
- Persistent loop with cumulative round/worker/wall/idle budgets, pre-execution failed-fingerprint refusal, machine/path completion gates, independent final Verifier, and durable terminal commit ordering.
- Immutable Git baseline containing initial HEAD plus dirty index/content identity; later worktree, staged, deletion, and committed changes are checked fail-closed against allowed/forbidden paths.
- Safe omitted-verification defaults compile Python only. Ambiguous `pyproject.toml`, `package.json`, `Cargo.toml`, and `go.mod` commands are persisted as proposal-only `BLOCKED` contracts and never run without explicit `--verify-argv` approval.
- OS-backed canonical-workspace lock, crash-accounted `active_started_at`, and bounded resume context. Windows recovery rehydrates kernel creation identity before classifying PID reuse. Live or unverifiable orphan identity is a retryable exit `4` with no state/summary mutation; dead work is recorded `interrupted`.
- Completed Writer final/result checkpoints and final-review attempts can be recovered without replay. Partial Writer streams alone are evidence, not a completed action.
- AgentFlow-first adaptive Pack for qualified independent read-only missions, with native Scout fanout, bounded concurrency, one Scout retry, strict completed-result validation, stable expanded-node mapping, durable recovery, and direct fallback. A cancel-only periodic controller is policy- and action-audited where platform-safe; Windows keeps live culling in the guarded direct backend. JSONL progress events cannot masquerade as Scout finals.
- Codex capability negotiation requires `approval=never`, `--ignore-user-config`, role sandboxing, and native Windows `elevated` isolation. Production prompts use stdin; complete redacted output stays on disk while memory context remains byte-bounded.
- Real Codex receives an ephemeral authentication copy plus a snapshot of non-system user skills in an isolated `CODEX_HOME`; fake prefixes receive neither. Global config and `.system` skills are excluded. Normal exit deletes the isolated home, and the guarded stale-home reaper requires a Hound marker, a direct Temp child, and a dead owner PID.
- Windows launches pin Hound's known-good Python, create no console window, and suppress process-error dialogs. Cancellation uses only guarded direct-worker PID-tree `System32\taskkill.exe /PID <pid> /T /F`; no console-control event API is present.
- `install-skill` defaults to the user-wide `.agents/skills` directory, keeps project-local installation behind `--project`, is idempotent for owned content, and preserves overwrite/removal ownership guards. `doctor` reports both scopes.

## Current evidence

- Full suite: 167 tests collected; all passed with warnings promoted to errors, exit `0`.
- `pyproject.toml` declares no mandatory runtime dependencies; `pytest>=8` is test-only.
- Static Windows termination boundary: guarded exact worker PID-tree `taskkill.exe`; `CTRL_C_EVENT`, `CTRL_BREAK_EVENT`, and `GenerateConsoleCtrlEvent` remain forbidden.
- Final source: 20 Python modules, 5,559 physical lines, 267,912 bytes. Largest modules are `process.py` 762, `engine.py` 604, and `pack.py` 524 lines; the 500-line target remains soft.
- Final wheel: 76,853 bytes, SHA-256 `1A57A0B0DE292A0A16F08FE36C35127802562EA3B90BB507CB4AA5B49356BFA9`. Final sdist: 108,360 bytes, SHA-256 `B219DDDE25AA5204664F1BE099CAD5EBD0AB32CA8919A7D3466D0E3F615AB6DB`. Both carry `License-Expression: MIT`, `LICENSE`, and `NOTICE.md`; the sdist includes the skill source plus all test helpers and passes the full suite in isolation.
- The generated culling pipeline validates against installed AgentFlow 0.1.0 and expands to two read-only Scouts plus one retry-free read-only controller. A fake native action round-trip proves confirmed culls, outer-cancel distinction, durable recovery, and policy rejection without launching unsafe Windows cancellation.
- Release audit run `20260826-235755-aabe1f86` passed Hound's authoritative pytest and compile gates and used AgentFlow run `91e1b6ca6f334a0b98146b25b6254d99`. It exposed a native completed node whose JSONL progress event had been mistaken for a Scout final; strict schema validation now rejects that result, with a focused regression test.
- Read-only acceptance run `20260827-001518-d3f27f1c` proved the authoritative gates pass but exposed a Writer handoff ambiguity: worker-local Python was unavailable, so the Writer returned `continue` even though Hound then passed both gates. The common prompt now defines `candidate_done` as a request for Hound-run gates and forbids duplicating them; a focused regression locks that contract.
- Self-host discovery/fix run `20260824-141347-5ec8034b` used AgentFlow runs `ba7f91434473461386c80bb114b0307f` and `afba20e5e88043e1b89ce54c56919c94`, then fixed the Windows live-worker creation-time hydration gap with one focused regression. Its evidence-first three-round contract exhausted before final review; the follow-up read-only acceptance run supplies the terminal verdict.
- Real Codex Solo run `20260824-035223-df426218`: one Writer plus one read-only Verifier, machine verification passed, Verifier accepted, durable `DONE`, exit `0`, and only `answer.py` changed.
- Real AgentFlow run `6e6728ed4c074e3cbe27dea82aea2d46`: two independent read-only Scouts completed and reported `alpha = 1` and `beta = 2`; `facts.txt` stayed byte-identical.
- Lyriq run `20260824-152648-a5ddf5a3` exposed the defect: the Writer and three Scouts could not see the global `lyriq-porting-safety` skill, changed no device or source state, and the Pack was cancelled after `556.562` active seconds.
- Real skill-projection run `20260824-170605-7e66529c`: isolated Codex loaded `lyriq-porting-safety`, reported serial `ZY22J58799` and default experimental slot B, passed machine verification and the read-only Verifier, and committed durable `DONE` without device, WSL, network, or target-file edits.
- Final timeout safety run: owner PID `54288` terminated only worker PID `47464` and child `conhost.exe` PID `31652` through exact PID-tree `taskkill`, exit `0`. Explorer PID `58620` and Codex app-server PID `28472` survived.
- Current config SHA-256 remained `D09A489AD39445E0BC888C31A57EAA90B5E95D470A35AC360A8DB0854AFB44E2` before and after the final isolated-wheel checks.
- User-wide `C:\Users\Administrator\.agents\skills\hounds\SKILL.md` is byte-identical to the repository source and embedded installer text; default install is idempotent and `doctor` reports user-wide `true`, project-local `false`.
- Full command and artifact evidence: `docs/completion-audit.md`.

## Deliberate limits

- Codex is the only adapter.
- AgentFlow provides optional bounded read-only execution and policy-audited native member culling off Windows. Windows live culling remains guarded direct-only until AgentFlow exposes a compatible exact PID-tree hook.
- Windows shutdown is intentionally forceful after strict direct-child and ancestor validation.
- Hound remains local CLI infrastructure: no daemon, UI, cloud runner, public DAG, provider manager, or parallel code writers.
