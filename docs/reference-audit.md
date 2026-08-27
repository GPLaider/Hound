# Reference audit

Audited 2026-08-22 from read-only clones:

- LazyCodex `10f95587d3aeacf208cc1fee88a91315962d31e8`, MIT
- pinned oh-my-openagent submodule `65715d1c2c35e27ccf2195ef688b0909dddb403c`, Sustainable Use License 1.0 (`SUL-1.0`)
- AgentFlow `ac59fa429b9dd0067933e1cb14b560fa32363be3`, MIT
- installed AgentFlow `0.1.0` under `C:\Users\Administrator\.agentflow` (read-only)

| Feature | LazyCodex / core | AgentFlow | Hound | Lightweight form |
|---|---|---|---|---|
| continuation loop | `$ulw-loop`, stop-hook continuation | iterative graph cycles | yes | bounded fixed rounds |
| completion promise/contract | goal/Oracle verification | success criteria | yes | persisted `RunContract` |
| durable progress | Boulder/loop state | run store | yes | atomic JSON/JSONL + crash-accounted budget |
| checklist/plan resume | Boulder plan progress | resume/rerun | yes | last durable round, no DAG |
| verify before completion | Oracle/evidence gates | success evaluators | yes | machine gate + Verifier |
| parallel spawn/fan-out | specialist/background workers | graph fan-out | conditional | AgentFlow-first for qualified read-only missions; direct fallback |
| worker cancellation | background task lifecycle | runner cancellation | yes | guarded whole AgentFlow PID tree; direct per-Scout culling |
| retry | continuation loop | node retries | yes | bounded Writer rounds; one AgentFlow node retry |
| partial artifacts/events | session evidence | stdout/stderr/trace/events | yes | Hound artifacts plus imported AgentFlow node records |
| single writer/worktrees | deep worker discipline | optional worktrees | strict single Writer | workspace lock, no fan-out writes |
| model routing | role profiles | per-agent provider/model | optional | inherit Codex; role override strings |
| hooks | many lifecycle hooks | runner lifecycle | no | explicit engine calls |
| graph optimizer/DAG DSL | no public Python DAG | central feature | internal Pack only | generated fixed-shape JSON; no public DAG |
| cloud runners | installer/provider ecosystem | Docker/SSH/AWS/SkyPilot | no | local subprocess only |
| web UI/TUI/daemon | installer/docs surfaces | web server/UI | no | CLI only |

Hound reimplements principles rather than copying reference source. The two top-level references are MIT; LazyCodex's pinned orchestration core is `SUL-1.0`. Exact attribution and the no-source-copied statement are in `NOTICE.md`.
