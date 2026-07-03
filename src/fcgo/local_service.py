from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Literal

ServiceAction = Literal["start", "stop", "restart", "status"]


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def service_logs(workspace: Path | None = None) -> tuple[Path, Path]:
    root = workspace or workspace_root()
    return root / "server.out.log", root / "server.err.log"


def service_control_logs(workspace: Path | None = None) -> tuple[Path, Path]:
    root = workspace or workspace_root()
    return root / "service-control.out.log", root / "service-control.err.log"


def default_port() -> int:
    return int(os.environ.get("FCGO_RESTART_PORT", os.environ.get("FCGO_PORT", "8000")))


def service_status(
    *,
    workspace: Path | None = None,
    port: int | None = None,
    assume_http_running: bool = False,
) -> dict[str, Any]:
    root = workspace or workspace_root()
    service_port = port or default_port()
    pids = fcgo_server_pids()
    health = True if assume_http_running else _health_ok(service_port)
    port_busy = _port_in_use(service_port)
    stdout_log, stderr_log = service_logs(root)
    if health:
        state = "running"
        message = "服务运行正常"
    elif pids:
        state = "starting"
        message = "检测到服务进程，健康检查还未就绪；如果持续超过 30 秒，请重启服务"
    elif port_busy:
        state = "blocked"
        message = f"端口 {service_port} 已被其他程序占用，请关闭占用程序或修改 FCGO_PORT"
    else:
        state = "stopped"
        message = "服务未运行"
    return {
        "state": state,
        "running": state == "running",
        "health": "ok" if health else "unavailable",
        "port": service_port,
        "url": f"http://127.0.0.1:{service_port}/healthz",
        "pids": pids,
        "port_in_use": port_busy,
        "message": message,
        "stdout": str(stdout_log),
        "stderr": str(stderr_log),
    }


def run_service_action(
    action: ServiceAction,
    *,
    workspace: Path | None = None,
    port: int | None = None,
    startup_timeout: int | None = None,
) -> dict[str, Any]:
    root = workspace or workspace_root()
    service_port = port or default_port()
    timeout = startup_timeout or int(os.environ.get("FCGO_RESTART_TIMEOUT_SECONDS", "60"))
    if action == "status":
        return service_status(workspace=root, port=service_port)
    if action == "stop":
        stopped = stop_service()
        status = service_status(workspace=root, port=service_port)
        return {"action": action, "stopped_pids": stopped, **status}
    if action == "start":
        result = start_service(workspace=root, port=service_port, startup_timeout=timeout)
        return {"action": action, **result}
    if action == "restart":
        stopped = stop_service()
        result = start_service(workspace=root, port=service_port, startup_timeout=timeout)
        return {"action": action, "stopped_pids": stopped, **result}
    raise ValueError(f"unknown service action: {action}")


def trigger_service_action(
    action: ServiceAction,
    *,
    delay_seconds: float = 1.0,
    workspace: Path | None = None,
) -> dict[str, Any]:
    if action not in {"start", "stop", "restart"}:
        raise ValueError("service action must be start, stop, or restart")
    root = workspace or workspace_root()
    script = root / "scripts" / "restart_server.py"
    if not script.exists():
        raise RuntimeError(f"service script not found: {script}")
    stdout_log, stderr_log = service_control_logs(root)
    stdout_handle = stdout_log.open("a", encoding="utf-8")
    stderr_handle = stderr_log.open("a", encoding="utf-8")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [
            sys.executable,
            str(script),
            action,
            "--delay",
            str(delay_seconds),
        ],
        cwd=root,
        stdout=stdout_handle,
        stderr=stderr_handle,
        creationflags=creationflags,
    )
    stdout_handle.close()
    stderr_handle.close()
    return {
        "action": action,
        "pid": proc.pid,
        "stdout": str(stdout_log),
        "stderr": str(stderr_log),
        "message": _trigger_message(action),
    }


def start_service(
    *,
    workspace: Path | None = None,
    port: int | None = None,
    startup_timeout: int = 60,
) -> dict[str, Any]:
    root = workspace or workspace_root()
    service_port = port or default_port()
    status = service_status(workspace=root, port=service_port)
    if status["state"] == "running":
        return status
    if status["pids"]:
        raise RuntimeError(status["message"])
    if status["port_in_use"]:
        raise RuntimeError(status["message"])

    stdout_log, stderr_log = service_logs(root)
    stdout_log.write_text("", encoding="utf-8")
    stderr_log.write_text("", encoding="utf-8")
    python = _service_python(root)
    stdout_handle = stdout_log.open("a", encoding="utf-8")
    stderr_handle = stderr_log.open("a", encoding="utf-8")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [str(python), "-m", "fcgo.cli", "serve"],
        cwd=root,
        stdout=stdout_handle,
        stderr=stderr_handle,
        creationflags=creationflags,
    )
    stdout_handle.close()
    stderr_handle.close()
    health_url = f"http://127.0.0.1:{service_port}/healthz"
    _wait_until_healthy(proc, health_url, startup_timeout)
    return service_status(workspace=root, port=service_port) | {"pid": proc.pid}


def stop_service() -> list[int]:
    pids = fcgo_server_pids()
    for pid in pids:
        _kill_process(pid)
    _wait_until_stopped()
    return pids


def fcgo_server_pids() -> list[int]:
    if os.name == "nt":
        return _windows_fcgo_server_pids()
    return _posix_fcgo_server_pids()


def _service_python(workspace: Path) -> Path:
    python = workspace / ".venv" / "Scripts" / "python.exe"
    if python.exists():
        return python
    return Path(sys.executable)


def _windows_fcgo_server_pids() -> list[int]:
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
    return _parse_pids(completed.stdout)


def _posix_fcgo_server_pids() -> list[int]:
    completed = subprocess.run(
        ["pgrep", "-f", "--", "-m fcgo.cli serve"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in {0, 1}:
        raise RuntimeError(completed.stderr.strip() or "failed to inspect processes")
    return [int(line) for line in completed.stdout.splitlines() if line.strip().isdigit()]


def _parse_pids(output: str) -> list[int]:
    text = output.strip()
    if not text:
        return []
    parsed: Any = json.loads(text)
    if parsed is None:
        return []
    if isinstance(parsed, int):
        return [parsed]
    return [int(pid) for pid in parsed]


def _kill_process(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    subprocess.run(
        ["kill", "-TERM", str(pid)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_until_stopped() -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not fcgo_server_pids():
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
        if _health_url_ok(health_url):
            return
        if proc.poll() is not None and not fcgo_server_pids():
            raise RuntimeError(f"fcgo server exited early with code {proc.returncode}")
        time.sleep(0.8)
    if _health_url_ok(health_url):
        return
    raise RuntimeError(f"fcgo server did not become healthy at {health_url}")


def _health_ok(port: int) -> bool:
    return _health_url_ok(f"http://127.0.0.1:{port}/healthz")


def _health_url_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("status") == "ok"
    except Exception:
        return False


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def _trigger_message(action: str) -> str:
    if action == "restart":
        return "已触发重启，几秒后刷新页面查看状态"
    if action == "stop":
        return "已触发停止，页面随后可能无法继续访问"
    return "已触发启动"
