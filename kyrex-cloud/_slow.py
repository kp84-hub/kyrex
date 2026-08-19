
import sys, json, time
for i in range(2):
    sys.stdout.write("KYREX_APPROVAL:" + json.dumps(
        {"tier": 1, "summary": "op %d" % i, "token": ""}) + "\n")
    sys.stdout.flush()
    sys.stdin.readline()
sys.stdout.write("KYREX_RESULT_JSON:" + json.dumps(
    {"status": "no_changes", "final_response": "survived"}) + "\n")
sys.stdout.flush()
