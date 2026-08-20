
import sys, json
req = {"tier": 1, "summary": "test op", "token": ""}
sys.stdout.write("KYREX_APPROVAL:" + json.dumps(req) + "\n")
sys.stdout.flush()
decision = sys.stdin.readline().strip()
sys.stdout.write("KYREX_RESULT_JSON:" + json.dumps(
    {"status": "no_changes", "final_response": "decision=" + decision}) + "\n")
sys.stdout.flush()
