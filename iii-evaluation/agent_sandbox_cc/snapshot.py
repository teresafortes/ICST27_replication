"""PostToolUse hook (managed settings): after every tool call, save program.dfy and
diagnosis.json to /snap, so the runner knows when each file first changed. Never fails
the tool call."""
import json, sys, time
from pathlib import Path

try:
    event = json.load(sys.stdin)
    def read(p):
        try:
            return Path(p).read_text()
        except (OSError, UnicodeDecodeError):
            return None
    Path("/snap", f"{time.time_ns()}.json").write_text(json.dumps({
        "tool_use_id": event.get("tool_use_id"), "tool_name": event.get("tool_name"),
        "program": read("/work/program.dfy"), "diagnosis_json": read("/work/diagnosis.json")}))
except Exception:
    pass
sys.exit(0)
