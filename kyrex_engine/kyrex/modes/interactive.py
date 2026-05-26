import sys
import asyncio
from ..pager import pager

THINK_COLOR = "\033[38;5;67m\033[3m"
RST = "\033[0m"

def run_interactive(engine):
    def reasoning_callback(chunk):
        sys.stdout.write(f"{THINK_COLOR}{chunk}{RST}")
        sys.stdout.flush()

    def stream_callback(chunk):
        sys.stdout.write(chunk)
        sys.stdout.flush()

    engine._stream_handler = stream_callback
    engine._reasoning_handler = reasoning_callback

    while True:
        try:
            branch = engine.session.current_branch_name
            user_input = input(f"\n(px:{branch}) > ")
            if user_input.lower() in ("exit", "quit"):
                break
            sys.stdout.write("\n")
            result, _ = asyncio.run(engine.chat(user_input))
        except KeyboardInterrupt:
            break
