# 便携启动包

FCGO 当前提供便携启动包作为安装器 MVP。它不是完整图形安装器，但已经可以把项目打成
Windows/macOS/Linux 可解压运行的目录和压缩包。

## 生成

生成当前系统平台的包：

```bash
uv run fcgo package build
```

生成所有平台脚本包：

```bash
uv run fcgo package build --target all
```

产物会输出到 `dist/`：

- `fcgo-portable-windows/` 和 `fcgo-portable-windows.zip`
- `fcgo-portable-macos/` 和 `fcgo-portable-macos.tar.gz`
- `fcgo-portable-linux/` 和 `fcgo-portable-linux.tar.gz`

## 启动

Windows：

```text
双击 start-fcgo.cmd
```

macOS/Linux：

```bash
./start-fcgo.sh
```

启动脚本会：

1. 如果没有 `.env`，从 `.env.example` 复制一个。
2. 执行 `uv sync` 安装依赖。
3. 执行 `uv run fcgo service start --open-admin`。
4. 打开本地配置后台，由用户继续完成首次配置向导。

## 安全边界

打包脚本不会复制：

- `.env`
- `.venv`
- `data/`
- `logs/`
- `server*.log`
- `service-control*.log`
- PowerShell 脚本

因此本机密钥、数据库和日志不会进入分发包。

## 当前限制

- 用户仍需要先安装 Python 3.13 和 uv。
- Windows 当前是 `start-fcgo.cmd` 便携启动包，不是 MSI/EXE 图形安装器。
- macOS/Linux 当前是 shell 启动包，不是 `.dmg`、`.pkg`、`.deb` 或 AppImage。

后续完整安装器可以继续复用 `fcgo package build` 的产物和 `fcgo service` 控制能力。
