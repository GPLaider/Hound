from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def emit(value: dict) -> None:
    print(json.dumps(value), flush=True)


def main() -> int:
    role, prompt = sys.argv[1:3]
    scenario = os.environ.get("HOUND_FAKE_SCENARIO", "good")
    root = Path.cwd()
    if scenario == "error":
        print("fake error", file=sys.stderr, flush=True)
        return 7
    if scenario == "invalid-json":
        print("not json", flush=True)
        return 0
    if scenario == "writer-timeout" and role == "writer":
        print("partial timeout evidence", flush=True)
        time.sleep(30)
        return 0
    if scenario == "interrupted" and role == "writer":
        marker = root / ".fake-interrupted"
        if not marker.exists():
            marker.write_text("started", encoding="ascii")
            print("partial before orchestrator crash", flush=True)
            time.sleep(0.5)
            return 0
        emit({"status": "candidate_done", "summary": "resumed", "evidence": [{"claim": "resume"}],
              "changed_files": [], "next_action": "done", "parallel_request": {"needed": False}})
        return 0
    if scenario == "interrupted" and role == "verifier":
        emit({"verdict": "accept", "verified_claims": ["resume and machine gate"],
              "unverified_claims": [], "blocking_issues": [], "nonblocking_issues": [],
              "required_next_action": None})
        return 0
    if scenario == "blocked" and role == "writer":
        emit({"status": "blocked", "summary": "needs external input", "evidence": [],
              "changed_files": [], "next_action": "", "parallel_request": {"needed": False},
              "blocker": "missing external credential"})
        return 0
    if scenario == "commit-forbidden" and role == "writer":
        (root / "protected.txt").write_text("changed\n", encoding="utf-8")
        subprocess.run(["git", "add", "protected.txt"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "forbidden writer change"], check=True)
        emit({"status": "candidate_done", "summary": "committed", "evidence": [{"claim": "commit"}],
              "changed_files": ["protected.txt"], "next_action": "done",
              "parallel_request": {"needed": False}})
        return 0
    if scenario == "child-process":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (root / "child.pid").write_text(str(child.pid), encoding="ascii")
        time.sleep(60)
        return 0
    if scenario == "single-writer" and role == "writer":
        active = root / "writer-active"
        if active.exists():
            (root / "writer-overlap").write_text("overlap", encoding="ascii")
        active.write_text(str(os.getpid()), encoding="ascii")
        time.sleep(0.15)
        active.unlink(missing_ok=True)
        emit({"status": "candidate_done", "summary": "serialized", "evidence": [{"claim": "lock"}],
              "changed_files": ["writer-active"], "next_action": "done", "parallel_request": {"needed": False}})
        return 0
    if scenario == "single-writer" and role == "verifier":
        emit({"verdict": "accept", "verified_claims": ["machine gate"], "unverified_claims": [],
              "blocking_issues": [], "nonblocking_issues": [], "required_next_action": None})
        return 0
    if scenario in {"verification-loop", "agentflow-loop", "direct-loop"}:
        if role == "writer":
            marker = root / ".fake-round"
            if marker.exists():
                if scenario == "direct-loop":
                    (root / ".second-writer-prompt").write_text(prompt, encoding="utf-8")
                (root / "answer.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
                emit({"status": "candidate_done", "summary": "fixed", "evidence": [{"claim": "answer is 42"}],
                      "changed_files": ["answer.py"], "next_action": "verify", "parallel_request": {"needed": False}})
            else:
                marker.write_text("1", encoding="ascii")
                (root / "answer.py").write_text("def answer():\n    return 0\n", encoding="utf-8")
                emit({"status": "continue", "summary": "first attempt", "evidence": [{"claim": "candidate"}],
                      "changed_files": ["answer.py"], "next_action": "fix failed test",
                      "parallel_request": {"needed": scenario == "agentflow-loop",
                                           "missions": ["trace failure", "disprove assumption"]}})
            return 0
        if role == "verifier":
            emit({"verdict": "accept", "verified_claims": ["machine gate"], "unverified_claims": [],
                  "blocking_issues": [], "nonblocking_issues": [], "required_next_action": None})
            return 0
    if scenario == "completed-good-stalled" and role == "scout":
        if "reproduce" in prompt:
            emit({"type": "evidence", "claim": "completed survivor"})
            emit({"summary": "completed survivor", "evidence": [{"claim": "root cause"}],
                  "hypotheses": [], "recommended_next_action": "fix root cause",
                  "unique_contribution": "completed evidence", "confidence": 0.9})
            return 0
        time.sleep(60)
        return 0
    if scenario == "completed-low-stalled-high" and role == "scout":
        if "reproduce" in prompt:
            emit({"summary": "done", "evidence": [], "hypotheses": [],
                  "recommended_next_action": "", "unique_contribution": "", "confidence": 0.1})
            return 0
        for index in range(6):
            emit({"type": "evidence", "claim": f"live evidence {index}",
                  "path": f"src/file{index}.py", "exit_code": 1})
        time.sleep(60)
        return 0
    if role == "scout":
        if "reproduce" in prompt:
            emit({"type": "evidence", "path": "src/core.py", "exit_code": 1, "claim": "reproduced error"})
            emit({"type": "evidence", "path": "tests/test_core.py", "claim": "verified root cause"})
            time.sleep(2 if scenario == "pack" else 0.5)
            emit({"summary": "good", "evidence": [{"claim": "root cause", "source": "src/core.py"}],
                  "hypotheses": [], "recommended_next_action": "fix core", "unique_contribution": "reproduction", "confidence": 0.9})
        elif "trace" in prompt:
            print("noise\nnoise\nnoise", flush=True)
            time.sleep(4 if scenario == "pack" else 2)
            emit({"summary": "duplicate", "evidence": [], "hypotheses": [], "recommended_next_action": "", "confidence": 0.2})
        else:
            time.sleep(60)
        return 0
    emit({"status": "candidate_done", "summary": "good", "evidence": [{"claim": "done"}],
          "changed_files": [], "next_action": "done", "parallel_request": {"needed": False}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
