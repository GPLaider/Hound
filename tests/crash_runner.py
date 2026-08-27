from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from hounds.adapter import CodexAdapter
from hounds.engine import Engine
from hounds.models import Budget, PackPolicy, RunContract


class SelfCrashAdapter(CodexAdapter):
    async def start(self, *args, **kwargs):
        managed, final = await super().start(*args, **kwargs)
        stdout = managed.artifact_dir / "stdout.log"
        for _ in range(100):
            if stdout.exists() and stdout.stat().st_size:
                break
            await asyncio.sleep(0.01)
        os._exit(86)


async def main() -> None:
    root = Path(sys.argv[1]).resolve()
    fake = Path(sys.argv[2]).resolve()
    engine = Engine(root, SelfCrashAdapter(prefix=[sys.executable, str(fake)]), {})
    contract = RunContract("resume after crash", str(root), [[sys.executable, "-V"]],
                           budget=Budget(max_rounds=3), pack=PackPolicy(mode="off"))
    state = engine.new_run(contract)
    (root / "run-id.txt").write_text(state.run_id, encoding="ascii")
    await engine.run(state, contract)


if __name__ == "__main__":
    asyncio.run(main())
