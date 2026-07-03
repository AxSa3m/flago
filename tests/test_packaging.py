from __future__ import annotations

import os
from pathlib import Path

from fcgo.packaging import build_portable_package


def test_build_windows_portable_package_excludes_local_secrets(tmp_path: Path) -> None:
    workspace = _fake_workspace(tmp_path)
    result = build_portable_package(
        workspace=workspace,
        dist_dir=tmp_path / "dist",
        target="windows",
    )

    package_dir = Path(result["package_dir"])
    launcher = Path(result["launcher"])

    assert package_dir.exists()
    assert launcher.name == "start-fcgo.cmd"
    assert Path(result["archive"]).suffix == ".zip"
    assert (package_dir / ".env.example").exists()
    assert not (package_dir / ".env").exists()
    assert not (package_dir / "data").exists()
    assert not (package_dir / "server.err.log").exists()
    assert "uv run fcgo service start --open-admin" in launcher.read_text(encoding="utf-8")
    assert "powershell" not in launcher.read_text(encoding="utf-8").lower()


def test_build_unix_portable_package_marks_launcher_executable(tmp_path: Path) -> None:
    workspace = _fake_workspace(tmp_path)
    result = build_portable_package(
        workspace=workspace,
        dist_dir=tmp_path / "dist",
        target="linux",
    )

    package_dir = Path(result["package_dir"])
    launcher = Path(result["launcher"])

    assert launcher.name == "start-fcgo.sh"
    assert Path(result["archive"]).name.endswith(".tar.gz")
    if os.name != "nt":
        assert (launcher.stat().st_mode & 0o111) != 0
    assert (package_dir / "README-portable.md").exists()
    assert "完整图形安装器会在后续版本补齐" in (
        package_dir / "README-portable.md"
    ).read_text(encoding="utf-8")


def _fake_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "src" / "fcgo").mkdir(parents=True)
    (workspace / "scripts").mkdir()
    (workspace / "docs").mkdir()
    (workspace / "data").mkdir()
    (workspace / "logs").mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='fcgo'\n", encoding="utf-8")
    (workspace / "uv.lock").write_text("", encoding="utf-8")
    (workspace / "README.md").write_text("# FCGO\n", encoding="utf-8")
    (workspace / ".env.example").write_text(
        "FCGO_BASE_URL=http://127.0.0.1:8000\n",
        encoding="utf-8",
    )
    (workspace / ".env").write_text("SECRET=do-not-copy\n", encoding="utf-8")
    (workspace / "server.err.log").write_text("local log\n", encoding="utf-8")
    (workspace / "src" / "fcgo" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "scripts" / "restart_server.py").write_text("print('ok')\n", encoding="utf-8")
    (workspace / "scripts" / "bad.ps1").write_text("Write-Host bad\n", encoding="utf-8")
    (workspace / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    if os.name != "nt":
        (workspace / "scripts" / "restart_server.py").chmod(0o755)
    return workspace
