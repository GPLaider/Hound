from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    if os.environ.get("HOUND_FAKE_AGENTFLOW_ERROR"):
        print("fake AgentFlow failure", file=sys.stderr)
        return 7
    args = sys.argv[1:]
    pipeline_path = Path(args[args.index("run") + 1])
    runs_dir = Path(args[args.index("--runs-dir") + 1])
    pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
    run_id = "fake-agentflow-run"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pipeline.json").write_text(json.dumps(pipeline, indent=2), encoding="utf-8")
    nodes = {}
    for index, node in enumerate(pipeline["nodes"], 1):
        assert node["tools"] == "read_only"
        output = json.dumps({
            "summary": f"evidence {index}",
            "evidence": [{"claim": f"fact {index}", "source": f"file-{index}.py",
                          "artifact": f"trace-{index}", "confidence": 0.8}],
            "hypotheses": [],
            "recommended_next_action": f"use fact {index}",
            "unique_contribution": f"path {index}",
            "confidence": 0.8,
        })
        nodes[node["id"]] = {
            "node_id": node["id"], "status": "completed", "exit_code": 0,
            "output": json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
            "final_response": output,
            "stdout_lines": [json.dumps({"type": "evidence", "index": index})],
            "stderr_lines": [],
            "trace_events": [{"kind": "evidence", "content": f"fact {index}"}],
        }
    record = {"id": run_id, "status": "completed", "pipeline": pipeline, "nodes": nodes}
    if os.environ.get("HOUND_FAKE_AGENTFLOW_STATUS_FAILED"):
        record["status"] = "failed"
        for node in nodes.values():
            node.update({"status": "failed", "exit_code": 1})
    if os.environ.get("HOUND_FAKE_AGENTFLOW_PARTIAL"):
        record["status"] = "running"
        last = nodes[pipeline["nodes"][-1]["id"]]
        last.update({"status": "running", "exit_code": None, "output": None,
                     "final_response": None, "trace_events": []})
        (run_dir / "run.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        time.sleep(60)
        return 0
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
