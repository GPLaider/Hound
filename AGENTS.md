# Hound repository rules

- Preserve one workspace-writing Writer. Scout, Judge, and Verifier remain read-only.
- Never use nested fan-out or invoke Hound from a Hound worker.
- Never modify Codex global `config.toml` or installed AgentFlow.
- Windows cancellation must target only a guarded direct worker PID tree with `taskkill.exe /PID <pid> /T /F`.
- Never use `CTRL_C_EVENT`, `CTRL_BREAK_EVENT`, `GenerateConsoleCtrlEvent`, or Windows `os.kill` for liveness checks.
- Completion requires machine verification, final Verifier acceptance, and durable artifacts.
