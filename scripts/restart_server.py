from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
SRC = WORKSPACE / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from flgo.local_service import run_service_action, service_logs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Control the local FLGO service")
    parser.add_argument(
        "action",
        nargs="?",
        choices=("start", "stop", "restart", "status"),
        default="restart",
        help="service action; default is restart for backward compatibility",
    )
    parser.add_argument("--delay", type=float, default=0.0, help="seconds to wait before action")
    parser.add_argument(
        "--open-admin",
        action="store_true",
        help="open the local admin page after a successful start or restart",
    )
    args = parser.parse_args()
    if args.delay > 0:
        time.sleep(args.delay)
    try:
        result = run_service_action(args.action, workspace=WORKSPACE)
        if args.open_admin and args.action in {"start", "restart"} and result.get("running"):
            webbrowser.open("http://127.0.0.1:8000/admin")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI script should print user-readable errors
        _, stderr_log = service_logs(WORKSPACE)
        payload = {
            "ok": False,
            "action": args.action,
            "error": str(exc),
            "stderr": str(stderr_log),
        }
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        _print_log_tail(stderr_log)
        return 1


def _print_log_tail(path: Path) -> None:
    if not path.exists():
        return
    print(f"---- {path.name} ----", file=sys.stderr)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-40:]:
        print(line, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
