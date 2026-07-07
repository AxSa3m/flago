from __future__ import annotations

import shutil
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

PackageTarget = Literal["windows", "macos", "linux"]

INCLUDE_FILES = ("pyproject.toml", "uv.lock", "README.md", ".env.example")
INCLUDE_DIRS = ("src", "scripts", "docs")
IGNORE_NAMES = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".uv-cache",
    "__pycache__",
    "build",
    "dist",
    "data",
    "logs",
}
IGNORE_SUFFIXES = (".pyc", ".pyo", ".log")


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def current_target() -> PackageTarget:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def build_portable_package(
    *,
    workspace: Path | None = None,
    dist_dir: Path | None = None,
    target: PackageTarget | None = None,
    clean: bool = True,
) -> dict[str, Any]:
    root = workspace or workspace_root()
    selected_target = target or current_target()
    output_root = dist_dir or root / "dist"
    package_name = f"flago-portable-{selected_target}"
    package_dir = output_root / package_name
    archive_base = output_root / package_name

    output_root.mkdir(parents=True, exist_ok=True)
    if clean and package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True, exist_ok=True)

    for name in INCLUDE_FILES:
        source = root / name
        if source.exists():
            shutil.copy2(source, package_dir / name)
    _prepare_portable_env_example(package_dir / ".env.example")

    for name in INCLUDE_DIRS:
        source = root / name
        if source.exists():
            shutil.copytree(source, package_dir / name, ignore=_ignore)

    _write_portable_readme(package_dir, selected_target)
    launcher = _write_launcher(package_dir, selected_target)
    archive = _make_archive(archive_base, package_dir, selected_target)
    return {
        "target": selected_target,
        "package_dir": str(package_dir),
        "archive": str(archive),
        "launcher": str(launcher),
    }


def _ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in IGNORE_NAMES or name.endswith(IGNORE_SUFFIXES):
            ignored.add(name)
    if Path(directory).name == "scripts":
        ignored.update(name for name in names if name.endswith(".ps1"))
    return ignored


def _write_launcher(package_dir: Path, target: PackageTarget) -> Path:
    if target == "windows":
        launcher = package_dir / "start-flago.cmd"
        launcher.write_text(_windows_launcher(), encoding="utf-8", newline="\r\n")
        return launcher

    launcher = package_dir / "start-flago.sh"
    launcher.write_text(_unix_launcher(), encoding="utf-8", newline="\n")
    with suppress(OSError):
        launcher.chmod(launcher.stat().st_mode | 0o755)
    return launcher


def _windows_launcher() -> str:
    return """@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set FLAGO_ENV=prod
set FLAGO_RESTART_TIMEOUT_SECONDS=120
set UV_LINK_MODE=copy

if not exist ".env" (
  copy ".env.example" ".env" >nul
)

where uv >nul 2>nul
if errorlevel 1 (
  echo 未找到 uv。请先安装 uv，然后重新双击本文件。
  echo 安装说明：https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)

uv sync
if errorlevel 1 (
  echo 依赖安装失败，请检查网络后重试。
  pause
  exit /b 1
)

uv run flago service start --open-admin
pause
"""


def _unix_launcher() -> str:
    return """#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

export FLAGO_ENV="${FLAGO_ENV:-prod}"
export FLAGO_RESTART_TIMEOUT_SECONDS="${FLAGO_RESTART_TIMEOUT_SECONDS:-120}"
export UV_LINK_MODE="${UV_LINK_MODE:-copy}"

if [ ! -f ".env" ]; then
  cp ".env.example" ".env"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "未找到 uv。请先安装 uv，然后重新运行本文件。"
  echo "安装说明：https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi

uv sync
uv run flago service start --open-admin
"""


def _prepare_portable_env_example(path: Path) -> None:
    if not path.exists():
        return
    replacements = {
        "FLAGO_ENV": "prod",
        "FLAGO_START_LONG_CONNECTION": "false",
        "FLAGO_ADMIN_SESSION_SECRET": "",
        "FEISHU_APP_ID": "",
        "FEISHU_APP_SECRET": "",
        "GEMINI_API_KEY": "",
        "OPENAI_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "QWEN_API_KEY": "",
        "DOUBAO_API_KEY": "",
        "MINIMAX_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "SEEDANCE_API_KEY": "",
        "COMFYUI_API_KEY": "",
        "COZE_API_KEY": "",
        "DIFY_API_KEY": "",
    }
    append_missing = {"FLAGO_ENV", "FLAGO_START_LONG_CONNECTION"}
    lines = path.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = (
            line.split("=", 1)[0].strip()
            if "=" in line and not line.lstrip().startswith("#")
            else ""
        )
        if key in replacements:
            updated.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            updated.append(line)
    for key, value in replacements.items():
        if key not in seen and key in append_missing:
            updated.append(f"{key}={value}")
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")


def _write_portable_readme(package_dir: Path, target: PackageTarget) -> None:
    if target == "windows":
        launch = "双击 `start-flago.cmd`"
        limitation = "Windows 当前提供便携启动包；完整图形安装器会在后续版本补齐。"
    else:
        launch = "在终端运行 `./start-flago.sh`"
        limitation = "macOS/Linux 当前提供便携启动包；完整图形安装器会在后续版本补齐。"

    text = f"""# Flago（FLAGO）便携启动包

## 这是什么

这是一个可直接启动Flago（FLAGO）的便携包。它不会包含你的本机 `.env`、数据库、日志或密钥。

## 启动前需要什么

- 已安装 Python 3.13。
- 已安装 uv。

## 怎么启动

1. 解压本目录。
2. {launch}。
3. 浏览器会打开本地配置后台。
4. 第一次使用时，按页面里的首次配置向导填写飞书应用和模型接口。

首次启动时会从 `.env.example` 自动复制出 `.env`。真实密钥只保存在你的本机目录里。

## 当前限制

{limitation}

如果启动失败，先在终端运行：

```bash
uv run flago service status
uv run flago doctor
```
"""
    (package_dir / "README-portable.md").write_text(text, encoding="utf-8")


def _make_archive(archive_base: Path, package_dir: Path, target: PackageTarget) -> Path:
    if target == "windows":
        archive_path = shutil.make_archive(
            str(archive_base),
            "zip",
            package_dir.parent,
            package_dir.name,
        )
    else:
        archive_path = shutil.make_archive(
            str(archive_base),
            "gztar",
            package_dir.parent,
            package_dir.name,
        )
    return Path(archive_path)
