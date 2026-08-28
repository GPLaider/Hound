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
    expanded = []
    for node in pipeline["nodes"]:
        fanout = node.get("fanout", {}).get("values")
        if fanout is not None:
            width = max(1, len(str(len(fanout))))
            expanded.extend(({**node, "id": f'{node["id"]}_{str(index).zfill(width)}',
                              "fanout": None}, item)
                            for index, item in enumerate(fanout))
        else:
            expanded.append((node, None))
    scout_index = 0
    for node, item in expanded:
        assert node["tools"] == "read_only"
        if item is None:
            nodes[node["id"]] = {
                "node_id": node["id"], "status": "completed", "exit_code": 0,
                "final_response": json.dumps({"analysis": "no cull", "actions": []}),
                "stdout_lines": [], "stderr_lines": [], "trace_events": [],
            }
            continue
        scout_index += 1
        output = json.dumps({
            "summary": f"evidence {scout_index}",
            "evidence": [{"claim": f"fact {scout_index}", "source": f"file-{scout_index}.py",
                          "artifact": f"trace-{scout_index}", "confidence": 0.8}],
            "hypotheses": [],
            "recommended_next_action": f"use fact {scout_index}",
            "unique_contribution": f"path {scout_index}",
            "confidence": 0.8,
        })
        nodes[node["id"]] = {
            "node_id": node["id"], "status": "completed", "exit_code": 0,
            "output": json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
            "final_response": output,
            "stdout_lines": [json.dumps({"type": "evidence", "index": scout_index})],
            "stderr_lines": [],
            "trace_events": [{"kind": "evidence", "content": f"fact {scout_index}"}],
        }
    record = {"id": run_id, "status": "completed", "pipeline": pipeline, "nodes": nodes}
    if os.environ.get("HOUND_FAKE_AGENTFLOW_CULL"):
        first = next(node_id for node_id in nodes if node_id.startswith("scouts_"))
        reason = "scouts_0 duplicates scouts_1 with score gap 12 and no unique evidence"
        nodes[first].update({"status": "cancelled", "exit_code": None})
        actions = {"analysis": "cull one duplicate", "actions": [
            {"kind": "cancel", "node_ids": [first], "reason": reason}]}
        controller = run_dir / "artifacts" / "scout_controller"
        controller.mkdir(parents=True, exist_ok=True)
        (controller / "periodic-actions-tick-2.json").write_text(
            json.dumps(actions), encoding="utf-8")
        event = {"timestamp": "2026-01-01T00:00:40+00:00", "run_id": run_id,
                 "type": "node_control_actions_applied", "node_id": "scout_controller",
                 "data": {"watched_group": "scouts", "actions": [
                     {"kind": "cancel", "node_id": first, "reason": reason}]}}
        (run_dir / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    if os.environ.get("HOUND_FAKE_AGENTFLOW_STATUS_FAILED"):
        record["status"] = "failed"
        for node in nodes.values():
            node.update({"status": "failed", "exit_code": 1})
    if os.environ.get("HOUND_FAKE_AGENTFLOW_PARTIAL"):
        record["status"] = "running"
        count = len(pipeline["nodes"][0]["fanout"]["values"])
        last = nodes[f'scouts_{str(count - 1).zfill(max(1, len(str(count))))}']
        last.update({"status": "running", "exit_code": None, "output": None,
                     "final_response": None, "trace_events": []})
        (run_dir / "run.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        time.sleep(60)
        return 0
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
