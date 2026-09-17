import sys
import asyncio
from pathlib import Path
from .core import PlaneExecute
from .config import ConfigManager
from .modes import run_interactive, run_rpc, run_print


def _run_decision_command(rest: list):
    """Developer-facing Jev probe. Read-only: cannot touch tools, files,
    Bots, or approvals. Returns before any engine construction."""
    if not rest or rest[0] != "test":
        print("usage: kyrex decision test --question risk --state \"...\"")
        return
    question = "risk"
    state = None
    i = 1
    while i < len(rest):
        if rest[i] == "--question" and i + 1 < len(rest):
            question = rest[i + 1]
            i += 2
        elif rest[i] == "--state" and i + 1 < len(rest):
            state = rest[i + 1]
            i += 2
        else:
            i += 1
    if state is None:
        print("usage: kyrex decision test --question risk --state \"...\"")
        return
    if question != "risk":
        print(f"  Only the 'risk' question is supported in this slice (got {question!r}).")
        return

    from .decision import JevClient, JevError, RISK_QUESTION, format_decision

    try:
        client = JevClient()
        result = client.decide(state, {"risk": RISK_QUESTION})
    except JevError as e:
        print(f"  Jev error: {e}")
        sys.exit(1)
    print(format_decision(result, question))


def main():
    args = sys.argv[1:]

    # decision dispatch happens before any setup/config gate: it reads only
    # TYPESAFE_API_KEY and never touches the generative-provider config.
    if args and args[0] == "decision":
        _run_decision_command(args[1:])
        return

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
