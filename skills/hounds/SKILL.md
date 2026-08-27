---
name: hounds
description: Use Hound for persistent verified Codex work that can call AgentFlow for bounded parallel read-only hunts when needed.
---

# Hound

- Do not use Hound for small or obvious changes.
- Start verified work with `hound run "objective" --verify-argv '["python","-m","pytest","-q"]'`.
- Hound keeps one Writer and uses AgentFlow only for a bounded parallel read-only hunt; direct Pack is a fallback.
- Hound isolates Codex worker config and pins workers to its trusted Python; never search application folders for runtimes.
- Inspect or continue durable work with `hound status`, `hound inspect <run-id>`, and `hound resume <run-id>`.
- Hound owns parallelism; never nest Codex native multi-agent work inside a Hound run.
- Do not enable dangerous sandbox access unless the user explicitly requests it.
