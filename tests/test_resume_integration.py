from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from hounds.store import RunStore, pid_alive


FAKE = Path(__file__).with_name("fake_agent.py")
CRASH_RUNNER = Path(__file__).with_name("crash_runner.py")


def test_actual_self_crash_resumes_without_killing_hound_or_worker(tmp_path: Path):
    async def check() -> None:
        env = os.environ.copy()
        env["HOUND_FAKE_SCENARIO"] = "interrupted"
        crashed = await asyncio.create_subprocess_exec(
            sys.executable, str(CRASH_RUNNER), str(tmp_path), str(FAKE),
            cwd=tmp_path, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(crashed.communicate(), 10)
        assert crashed.returncode == 86, (out + err).decode(errors="replace")
        run_id = (tmp_path / "run-id.txt").read_text(encoding="ascii")
        run_dir = RunStore(tmp_path).run_dir(run_id)
        launch = json.loads((run_dir / "rounds" / "001" / "writer" /
                             "launch.json").read_text())
        assert launch["hound_pid"] == crashed.pid
        assert launch["worker_ppid"] == crashed.pid
        assert launch["worker_pid"] not in {os.getpid(), crashed.pid}
        for _ in range(300):
            if not pid_alive(launch["worker_pid"]):
                break
            await asyncio.sleep(0.02)
        assert not pid_alive(launch["worker_pid"]), "worker did not exit naturally"

        env["HOUND_AGENT_PREFIX_JSON"] = json.dumps([sys.executable, str(FAKE)])
        resumed = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "hounds", "resume", run_id,
            cwd=tmp_path, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        resumed_out, resumed_err = await asyncio.wait_for(resumed.communicate(), 15)
        assert resumed.returncode == 0, (resumed_out + resumed_err).decode(errors="replace")
        state = RunStore(tmp_path).load_state(run_id)
        assert state.status.value == "done" and state.round == 2
        interrupted = json.loads((run_dir / "rounds" / "001" / "writer" /
                                  "interrupted.json").read_text())
        assert interrupted["worker_pid"] == launch["worker_pid"]
        assert "partial before orchestrator crash" in (
            run_dir / "rounds" / "001" / "writer" / "stdout.log").read_text()
        assert pid_alive(os.getpid())

    asyncio.run(check())
