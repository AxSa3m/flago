from __future__ import annotations

import json
import sys
import webbrowser
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
SRC = WORKSPACE / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from flgo.local_service import run_service_action  # noqa: E402


def main() -> int:
    result = run_service_action("start", workspace=WORKSPACE)
    url = "http://127.0.0.1:8000/admin"
    webbrowser.open(url)
    print(json.dumps({**result, "admin_url": url}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
