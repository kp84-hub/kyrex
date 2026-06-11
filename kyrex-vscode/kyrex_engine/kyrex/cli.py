import sys
import asyncio
from pathlib import Path
from .core import PlaneExecute
from .config import ConfigManager
from .modes import run_interactive, run_rpc, run_print


def main():
    args = sys.argv[1:]
    cfg = ConfigManager()
    cfg.load()

    if "--setup" in args or (args and args[0] == "setup"):
        cfg.setup_wizard()
        return

    if not cfg.config_path.exists():
        print("[!] No config found. Running setup...")
        cfg.setup_wizard()
        cfg.load()

    if args and args[0] == "doctor":
        cfg.show_status()
        return

    if not cfg.is_configured() and not args:
        print("  Kyrex is not configured yet.")
        want = input("  Run setup now? (Y/n): ").strip().lower()
        if want != "n":
            cfg.setup_wizard()
            cfg.load()
        if not cfg.is_configured():
            print("  No API key configured. Run `./kx --setup` or set KYREX_API_KEY.")
            return

    engine = PlaneExecute(config=cfg)

    if args and args[0] == "--orchestrate":
        print("[!] The orchestrator pipeline is not yet implemented.")
        print("    Mock stubs were removed. Real planner/critic/executor nodes required.")
        return

    if args and args[0] == "--rpc":
        run_rpc(engine)
        return

    if args and args[0] == "-p":
        prompt = " ".join(args[1:])
        mode = "json" if "--json" in args else "text"
        run_print(engine, prompt, mode)
        return

    if args:
        result, _ = asyncio.run(engine.chat(" ".join(args)))
        if result:
            from .pager import pager
            pager(result)
        return

    run_interactive(engine)


if __name__ == "__main__":
    main()
