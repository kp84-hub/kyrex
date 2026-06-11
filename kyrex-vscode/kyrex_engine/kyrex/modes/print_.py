import json
import asyncio


def run_print(engine, prompt: str, output_mode: str = "text"):
    result, _ = asyncio.run(engine.chat(prompt))
    if output_mode == "json":
        print(json.dumps({"status": "ok" if result is not None else "empty", "result": result}))
    elif result is not None:
        print(result)
