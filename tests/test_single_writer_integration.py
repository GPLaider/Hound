from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


FAKE = Path(__file__).with_name("fake_agent.py")


def test_two_hound_processes_never_overlap_writers(tmp_path: Path):
    async def check() -> None:
        env = os.environ.copy()
        env["HOUND_AGENT_PREFIX_JSON"] = json.dumps([sys.executable, str(FAKE)])
        env["HOUND_FAKE_SCENARIO"] = "single-writer"
        verify = json.dumps([sys.executable, "-V"])
        argv = [sys.executable, "-m", "hounds", "run", "serialize writer",
                "--pack", "off", "--max-rounds", "2", "--verify-argv", verify]
        first = await asyncio.create_subprocess_exec(
            *argv, cwd=tmp_path, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        marker = tmp_path / "writer-active"
        for _ in range(500):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.exists(), "first Writer never reached its guarded active window"
        second = await asyncio.create_subprocess_exec(
            *argv, cwd=tmp_path, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        second_out, second_err = await asyncio.wait_for(second.communicate(), 10)
        first_out, first_err = await asyncio.wait_for(first.communicate(), 10)
        assert first.returncode == 0, (first_out + first_err).decode(errors="replace")
        assert second.returncode == 4, (second_out + second_err).decode(errors="replace")
        assert b"lock already held" in second_err
        assert not (tmp_path / "writer-overlap").exists()

    asyncio.run(check())
