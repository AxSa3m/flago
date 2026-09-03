import socket
from pathlib import Path

from flago import local_service


def test_default_port_reads_workspace_env_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FLAGO_RESTART_PORT", raising=False)
    monkeypatch.delenv("FLAGO_PORT", raising=False)
    (tmp_path / ".env").write_text("FLAGO_PORT=8123\n", encoding="utf-8")

    assert local_service.default_port(tmp_path) == 8123


def test_windows_process_discovery_is_scoped_to_workspace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        local_service,
        "_windows_python_pids_using_workspace_modules",
        lambda workspace: [222],
    )
    monkeypatch.setattr(
        local_service.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("workspace lookup must not run the global process query")
        ),
    )

    assert local_service._windows_flago_server_pids(tmp_path, exclude_pid=None) == [222]


def test_port_in_use_detects_listener_bound_to_all_interfaces() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen()
        port = int(listener.getsockname()[1])

        assert local_service._port_in_use(port) is True


def test_service_status_reports_running(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_service, "flago_server_pids", lambda *args, **kwargs: [123])
    monkeypatch.setattr(local_service, "_health_ok", lambda port: True)
    monkeypatch.setattr(local_service, "_port_in_use", lambda port: True)

    status = local_service.service_status(workspace=tmp_path, port=8000)

    assert status["state"] == "running"
    assert status["running"] is True
    assert status["pids"] == [123]
    assert status["message"] == "服务运行正常"


def test_service_status_reports_blocked_port(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_service, "flago_server_pids", lambda *args, **kwargs: [])
    monkeypatch.setattr(local_service, "_health_ok", lambda port: False)
    monkeypatch.setattr(local_service, "_port_in_use", lambda port: True)

    status = local_service.service_status(workspace=tmp_path, port=8000)

    assert status["state"] == "blocked"
    assert status["running"] is False
    assert "端口 8000 已被其他程序占用" in status["message"]


def test_service_status_reports_starting_process(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_service, "flago_server_pids", lambda *args, **kwargs: [123])
    monkeypatch.setattr(local_service, "_health_ok", lambda port: False)
    monkeypatch.setattr(local_service, "_port_in_use", lambda port: True)

    status = local_service.service_status(workspace=tmp_path, port=8000)

    assert status["state"] == "starting"
    assert "健康检查还未就绪" in status["message"]


def test_service_status_can_assume_current_http_request_is_running(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(local_service, "flago_server_pids", lambda *args, **kwargs: [123])
    monkeypatch.setattr(
        local_service,
        "_health_ok",
        lambda port: (_ for _ in ()).throw(AssertionError("should not self-check")),
    )
    monkeypatch.setattr(local_service, "_port_in_use", lambda port: True)

    status = local_service.service_status(
        workspace=tmp_path,
        port=8000,
        assume_http_running=True,
    )

    assert status["state"] == "running"
    assert status["running"] is True
    assert status["health"] == "ok"
