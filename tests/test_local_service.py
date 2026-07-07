from pathlib import Path

from fcgo import local_service


def test_service_status_reports_running(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_service, "fcgo_server_pids", lambda *args, **kwargs: [123])
    monkeypatch.setattr(local_service, "_health_ok", lambda port: True)
    monkeypatch.setattr(local_service, "_port_in_use", lambda port: True)

    status = local_service.service_status(workspace=tmp_path, port=8000)

    assert status["state"] == "running"
    assert status["running"] is True
    assert status["pids"] == [123]
    assert status["message"] == "服务运行正常"


def test_service_status_reports_blocked_port(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_service, "fcgo_server_pids", lambda *args, **kwargs: [])
    monkeypatch.setattr(local_service, "_health_ok", lambda port: False)
    monkeypatch.setattr(local_service, "_port_in_use", lambda port: True)

    status = local_service.service_status(workspace=tmp_path, port=8000)

    assert status["state"] == "blocked"
    assert status["running"] is False
    assert "端口 8000 已被其他程序占用" in status["message"]


def test_service_status_reports_starting_process(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_service, "fcgo_server_pids", lambda *args, **kwargs: [123])
    monkeypatch.setattr(local_service, "_health_ok", lambda port: False)
    monkeypatch.setattr(local_service, "_port_in_use", lambda port: True)

    status = local_service.service_status(workspace=tmp_path, port=8000)

    assert status["state"] == "starting"
    assert "健康检查还未就绪" in status["message"]


def test_service_status_can_assume_current_http_request_is_running(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(local_service, "fcgo_server_pids", lambda *args, **kwargs: [123])
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
