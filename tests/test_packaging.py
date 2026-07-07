from __future__ import annotations

import os
from pathlib import Path

from flago.packaging import build_portable_package


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
    assert launcher.name == "start-flago.cmd"
    assert Path(result["archive"]).suffix == ".zip"
    assert (package_dir / ".env.example").exists()
    assert not (package_dir / ".env").exists()
    assert not (package_dir / "data").exists()
    assert not (package_dir / "server.err.log").exists()
    env_example = (package_dir / ".env.example").read_text(encoding="utf-8")
    launcher_text = launcher.read_text(encoding="utf-8")
    assert "FLAGO_ENV=prod" in env_example
    assert "FLAGO_START_LONG_CONNECTION=false" in env_example
    assert "FEISHU_APP_ID=\n" in env_example
    assert "FEISHU_APP_SECRET=\n" in env_example
    assert "GEMINI_API_KEY=\n" in env_example
    assert "cli_xxx" not in env_example
    assert "GEMINI_API_KEY=xxx" not in env_example
    assert "set FLAGO_ENV=prod" in launcher_text
    assert "set FLAGO_RESTART_TIMEOUT_SECONDS=120" in launcher_text
    assert "set UV_LINK_MODE=copy" in launcher_text
    assert "uv run flago service start --open-admin" in launcher_text
    assert "powershell" not in launcher_text.lower()


def test_build_unix_portable_package_marks_launcher_executable(tmp_path: Path) -> None:
    workspace = _fake_workspace(tmp_path)
    result = build_portable_package(
        workspace=workspace,
        dist_dir=tmp_path / "dist",
        target="linux",
    )

    package_dir = Path(result["package_dir"])
    launcher = Path(result["launcher"])

    assert launcher.name == "start-flago.sh"
    assert Path(result["archive"]).name.endswith(".tar.gz")
    if os.name != "nt":
        assert (launcher.stat().st_mode & 0o111) != 0
    launcher_text = launcher.read_text(encoding="utf-8")
    assert 'export FLAGO_ENV="${FLAGO_ENV:-prod}"' in launcher_text
    assert 'export FLAGO_RESTART_TIMEOUT_SECONDS="${FLAGO_RESTART_TIMEOUT_SECONDS:-120}"' in (
        launcher_text
    )
    assert 'export UV_LINK_MODE="${UV_LINK_MODE:-copy}"' in launcher_text
    assert (package_dir / "README-portable.md").exists()
    assert "完整图形安装器会在后续版本补齐" in (
        package_dir / "README-portable.md"
    ).read_text(encoding="utf-8")


def _fake_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "src" / "flago").mkdir(parents=True)
    (workspace / "scripts").mkdir()
    (workspace / "docs").mkdir()
    (workspace / "data").mkdir()
    (workspace / "logs").mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='flago'\n", encoding="utf-8")
    (workspace / "uv.lock").write_text("", encoding="utf-8")
    (workspace / "README.md").write_text("# Flago（FLAGO）\n", encoding="utf-8")
    (workspace / ".env.example").write_text(
        "\n".join(
            (
                "FLAGO_ENV=dev",
                "FLAGO_BASE_URL=http://127.0.0.1:8000",
                "FEISHU_APP_ID=cli_xxx",
                "FEISHU_APP_SECRET=xxx",
                "GEMINI_API_KEY=xxx",
                "",
            )
        ),
        encoding="utf-8",
    )
    (workspace / ".env").write_text("SECRET=do-not-copy\n", encoding="utf-8")
    (workspace / "server.err.log").write_text("local log\n", encoding="utf-8")
    (workspace / "src" / "flago" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "scripts" / "restart_server.py").write_text("print('ok')\n", encoding="utf-8")
    (workspace / "scripts" / "bad.ps1").write_text("Write-Host bad\n", encoding="utf-8")
    (workspace / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    if os.name != "nt":
        (workspace / "scripts" / "restart_server.py").chmod(0o755)
    return workspace
