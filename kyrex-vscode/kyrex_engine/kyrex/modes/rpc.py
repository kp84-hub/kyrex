import sys
import json


def run_rpc(engine):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            result, _ = engine.chat(request.get("input", ""))
            response = {"status": "ok", "result": result}
        except Exception as e:
            response = {"status": "error", "error": str(e)}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
