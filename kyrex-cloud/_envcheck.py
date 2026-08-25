import sys
sys.path.insert(0, "/tmp/kyrex-task-agent-1787689024-kyrex-cloud-finish-milestone-1-from/kyrex-cloud")
try:
    import fastapi, httpx
    print("fastapi+httpx OK", getattr(fastapi, "__version__", "?"))
except Exception as e:
    print("NO fastapi/httpx:", repr(e))
try:
    import task_store, worker
    print("worker+task_store import OK")
except Exception as e:
    print("worker import FAIL:", repr(e))
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("webmain", "/tmp/kyrex-task-agent-1787689024-kyrex-cloud-finish-milestone-1-from/kyrex-cloud/web/backend/main.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    print("web backend import OK")
except Exception as e:
    print("web backend import FAIL:", repr(e))
