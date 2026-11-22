# 刻度 · AI 用量账本

在 Windows 本机核对不同厂商、入口和模型的实际 Token 用量。选好日期和统计时区，就能从每日趋势和模型排行一路查到原始用量记录。项目、费用、额度和产出也可以留在本机。

**Windows x64 便携版**包含运行环境。解压到可写目录，双击 **启动账本.cmd**，默认浏览器会打开本机账本。首次启动会检查当前用户已有的 Antigravity 和 Codex 日志。[下载便携版](https://github.com/guofudamo2007-lab/ai-usage-ledger/releases/latest/download/Meter-Windows-x64.zip) · [发行记录与校验值](https://github.com/guofudamo2007-lab/ai-usage-ledger/releases/latest) · [使用与接入说明](使用说明.md)

![刻度总览，图中模型、项目与用量均为虚构演示数据](docs/overview-fictional.png)

预览使用独立的虚构账本。可以看日期选中、趋势悬停、排序和模型明细的实际操作。[交互录屏](docs/interaction-fictional.webp) · [窄窗口全图](docs/narrow-fictional.png)

## 能看什么

- **用量和排行**　支持今天、近 7 天、本月、全部历史和自定义日期。按模型、厂商或入口分组，可按总量、输出或剔除缓存读取后的用量排序。
- **模型明细**　查看同一模型在不同入口和项目中的分布，再按项目、入口或来源筛选逐条记录。CSV 和 JSON 导出覆盖当前条件下的所有分页。
- **产出账本**　按批次登记图像、视频、音频的可用数、重试、修整时间与实付金额。订阅、API 费用和剩余额度各自记账。

## 数据接入

| 接入方式 | 入口 |
| --- | --- |
| 本机只读采集 | Google Antigravity、Antigravity CLI、Antigravity IDE、OpenAI Codex。各入口分别显示可读范围和最近更新时间。 |
| 逐请求文件导入 | OpenAI API、Gemini API、Claude API 支持厂商响应包装或标准明细。Google AI Studio 需要能够确认入口归属的请求记录。Grok API、Kimi API 支持标准 JSON 或 CSV。 |
| 手工记账 | ChatGPT、Gemini、Claude、Grok、Kimi 网页可记费用、额度或产出。 |
| 仅供外部对账 | Obsidian Prism 汇总、按日合计报表、普通聊天导出。 |

输入包含可确认的缓存读写，输出包含可确认的推理。缺失字段显示为「—」，部分覆盖会列出已知条数。相同请求的重复导入可以补全缺失字段。剔除缓存读取的排行只使用总量与缓存读取均已知的记录。[字段口径与虚构格式示例](使用说明.md#计数与去重)

服务绑定本机回环地址。SQLite 数据库保存在程序目录的 `private/ledger.sqlite3`，保存统计字段、项目标签、采集进度、去重标识和手工账目。备份时关闭本地服务，复制整个 `private` 目录即可。[备份与迁移说明](使用说明.md#保存备份与迁移)

## 从源码运行

在装有 **Windows x64 CPython 3.14** 的机器上，从仓库根目录执行。前端资源已随源码提供。

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-cache-dir tzdata==2026.6
.\.venv\Scripts\python.exe -B -X utf8 ledger_app.py
```

使用 `--no-sync` 可以关闭启动和定时采集。测试会在项目目录中创建独立的临时数据库，使用虚构记录。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -B -X utf8 tools\package.py --runtime --archive
```

生成的便携包位于 `release/Meter-Windows-x64.zip`。本版使用 CPython 3.14.6 x64 和 tzdata 2026.6 构建。

## 许可证

刻度自身代码使用 MIT 许可证。前端组件和 IBM Plex Mono 字体分别保留上游许可证。Windows 发行包内的 CPython 及所带组件条款见 `runtime/LICENSE.txt`，时区数据条款在 `runtime/tzdata-licenses/`。[MIT](LICENSE) · [前端组件与设计来源](web/vendor/NOTICES.md) · [IBM Plex Mono 许可证](web/fonts/OFL.txt)
