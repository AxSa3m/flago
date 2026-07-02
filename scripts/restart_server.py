from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    port = int(os.environ.get("FCGO_RESTART_PORT", "8000"))
    startup_timeout = int(os.environ.get("FCGO_RESTART_TIMEOUT_SECONDS", "30"))
    stdout_log = workspace / "server.out.log"
    stderr_log = workspace / "server.err.log"
    python = workspace / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        print(f"restart failed: Python runtime not found: {python}", file=sys.stderr)
        return 1

    try:
        for pid in _fcgo_server_pids():
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        _wait_until_stopped()

        stdout_log.write_text("", encoding="utf-8")
        stderr_log.write_text("", encoding="utf-8")
        stdout_handle = stdout_log.open("a", encoding="utf-8")
        stderr_handle = stderr_log.open("a", encoding="utf-8")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [str(python), "-m", "fcgo.cli", "serve"],
            cwd=workspace,
            stdout=stdout_handle,
            stderr=stderr_handle,
            creationflags=creationflags,
        )
        health_url = f"http://127.0.0.1:{port}/healthz"
        _wait_until_healthy(proc, health_url, startup_timeout)
        print(
            json.dumps(
                {
                    "pid": proc.pid,
                    "health": "ok",
                    "url": health_url,
                    "stdout": str(stdout_log),
                    "stderr": str(stderr_log),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI script should print user-readable errors
        print(f"restart failed: {exc}", file=sys.stderr)
        _print_log_tail(stderr_log)
        _print_log_tail(stdout_log)
        return 1


def _fcgo_server_pids() -> list[int]:
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'python*' -and "
        "$_.CommandLine -like '*-m fcgo.cli serve*' } | "
        "Select-Object -ExpandProperty ProcessId | ConvertTo-Json"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "failed to inspect processes")
    output = completed.stdout.strip()
    if not output:
        return []
    parsed: Any = json.loads(output)
    if parsed is None:
        return []
    if isinstance(parsed, int):
        return [parsed]
    return [int(pid) for pid in parsed]


def _wait_until_stopped() -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _fcgo_server_pids():
            return
        time.sleep(0.3)
    raise RuntimeError("old fcgo server process did not stop within 10 seconds")


def _wait_until_healthy(
    proc: subprocess.Popen[bytes],
    health_url: str,
    startup_timeout: int,
) -> None:
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"fcgo server exited early with code {proc.returncode}")
        try:
            with urllib.request.urlopen(health_url, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "ok":
                return
        except Exception:
            time.sleep(0.8)
    raise RuntimeError(f"fcgo server did not become healthy at {health_url}")


def _print_log_tail(path: Path) -> None:
    if not path.exists():
        return
    print(f"---- {path.name} ----", file=sys.stderr)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-40:]:
        print(line, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
