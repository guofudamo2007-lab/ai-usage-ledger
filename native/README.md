# 刻度 · Rust 独立版

Windows x64 本地预览 0.2.0-preview。Rust 负责本地 HTTP 服务、日志采集、SQLite 存储、统计、估价和导出。网页资源、时区数据库、SQLite 及已编译的 WPF 小组件均随 EXE 分发。最终用户无需安装 Python、Rust 或 Node。

## 启动与数据目录

将 `Kedu-Rust-Windows-x64.exe` 放进一个可写文件夹，双击打开。默认使用回环端口 17963，占用时尝试后续 15 个端口。账本和设置保存在 EXE 旁的 `rust-private`，再次打开同一账本会复用正在运行的服务。原版可以同时运行，继续使用它自己的 `private`。首次使用 Rust 版会重新扫描仍可读取的本机来源。原版独有的旧日志历史、手工账目、自定义单价和更正记录仍在原版账本中。

点网页右上角「小组件」，或使用 `--widget` 直接打开桌面小组件。首次打开会把内置组件释放到 `rust-private/components`，需要 Windows 桌面版 .NET Framework 的 WPF 支持。关掉网页后服务会继续工作。在「数据接入」底部停止本地服务。备份时，停止服务后复制整个 `rust-private`。

```powershell
.\Kedu-Rust-Windows-x64.exe --widget
.\Kedu-Rust-Windows-x64.exe --data-dir ".\试用账本" --no-sync
```

可指定 `--port`、`--codex-home` 或 `--gemini-home`。如要给另一账号建立独立账本，请同时指定独立的 `--data-dir`。`--no-browser` 只启动服务，`--no-sync` 暂停自动采集，仍可手动刷新。

## 接入与核对

### 带入原版账本

1. 在原版「数据接入」底部点「停止本地服务」，或准备一份正常关闭后取得的完整 SQLite 备份。正在运行的原版可能还有数据只在 WAL 日志中，复制主文件可能漏掉这些记录。
2. 在 Rust 版打开「数据接入 → 导入原版账本」。点「选择文件」找到原版的 `private/ledger.sqlite3` 或备份，也可以直接粘贴完整文件路径。
3. 点「导入到 Rust 版」。结果分别列出历史用量、手工账目和自定义模型单价的新增、已有、跳过数量，以及补全的用量记录数。账本仍然独立，以后可以再次导入增量。

用量按请求身份核对，手工账目按原记录 ID 核对，自定义单价按型号核对。两边已经采集的同一调用只计一次。已有计数、账目内容或单价不一致时保留 Rust 版，结果中可查看跳过原因。原版待裁定的用量冲突可在原版核对后再导入。导入读取原版当前有效记录；旧导入批次、采集进度和自动额度快照不随账本导入。

源文件始终只读。遇到正在写入的账本或非空 WAL、回滚日志时会提示停止原版；请保留这些文件。一次导入的写入在 Rust 账本内整批提交，遇到数据库或格式错误会回滚。

- Codex 读取当前配置的 `sessions`、`archived_sessions`、可核实身份的索引路径和备份日志，支持 JSONL 与 Zstandard。普通日志在已提交的完整行处留存进度。索引中的累计值仅供对账。
- Antigravity、CLI、IDE 读取本机 SQLite 的步骤及生成元数据。保留来源观测，按请求身份去重，发生冲突时可以在「数据接入」核对并选取依据。清理源日志后会保留已入账事件。
- API 按现版逐请求 JSON/CSV 格式导入，可预检、相容补全、整批回滚或撤销。API 和本机采集使用各自的身份空间。筛选、时区、明细及导出采用同一批事件。缓存和推理计数属于各自父项，未知和零分别显示。
- 支持模型单价与价值排行、分项覆盖、外观设置、项目归并、账号和任务更正、费用与产出记录。离线估价表取自原版项目的同一份数据。
- Codex 和 AGY 额度查询使用已安装、已登录的官方 CLI。Copilot、MiniMax 与 Grok 根据本机 CLI 或客户端日志可用性读取。Claude Code 与 Kimi Code 支持额度快照导入。额度窗口缺失时保留未知。在线额度读取需要各来源自己的登录和网络条件。
- 当前验证环境是 Windows 开发机。原生客户端持续修订计数、真实订阅服务的实时返回和干净 Windows 整机断网仍需单独验证。

## 从源码构建

Windows x64 上需要 Rust 1.88 或更高版本的 MSVC 工具链、Visual C++ Build Tools、Windows SDK 及 .NET Framework 4.x 的 C# 编译器。首次构建时 Cargo 需要取得锁定的依赖。构建脚本编译仓库已有的小组件，并将它和网页资源编入 EXE。

```powershell
.\native\build.ps1
```

输出在 `release/rust/`。Rust 依赖版本由 `Cargo.lock` 固定。产品源码、界面和小组件沿用仓库许可证和各资源随附的许可。更新原版的内置标价或主题后，可运行 `python -B native/refresh_catalog.py` 重新生成静态目录。

## 验收

```powershell
python -B native/tests/accept.py release/rust/Kedu-Rust-Windows-x64.exe --ui
```

验收在项目内临时目录建立两个虚构账本，用原版作为独立参照核对 UID、总量、覆盖、价格和排行，并检查重复采集、导入撤销、导出和重启。测试读取原版参考实现需要 Python 3.14 及可用的 IANA 时区库。`--ui` 另需 Chrome、Playwright、pywin32 和 comtypes，并实际打开网页及 WPF 小组件。

