
import sys, json
sys.stdout.write("KYREX_APPROVAL:" + json.dumps(
    {"tier": 1, "summary": "op", "token": ""}) + "\n")
sys.stdout.flush()
sys.exit(1)
