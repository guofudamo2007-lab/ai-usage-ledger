# 干净 Windows 的整机断网验收

适用于独立 Windows x64 测试机，或带本地浏览器的全新虚拟机。预计 5 分钟。携带最终 ZIP 及同目录的 [三条虚构样本](断网验收-虚构样本.csv)。使用一个空的可写目录。

1. 在来宾或测试机运行 `Get-FileHash .\Meter-1.7-preview-Windows-x64.zip -Algorithm SHA256`，与[本次预发行的 SHA256SUMS.txt](https://github.com/guofudamo2007-lab/ai-usage-ledger/releases/download/v1.7-preview/SHA256SUMS.txt) 核对。完整解压。记录 `winver`、浏览器版本及包摘要。
2. 在虚拟机管理器中断开来宾的所有虚拟网卡，或在独立机拔网线并关闭 Wi-Fi、蜂窝及 VPN。保留本机回环。在来宾运行 `Test-NetConnection 1.1.1.1 -Port 443 -InformationLevel Quiet` 应返回 `False`，记录断网方式。整个测试期间保持这个状态。
3. 双击解压目录的 `启动账本.cmd`。用本机浏览器打开启动窗口给出的 `http://127.0.0.1:端口/`。在「数据接入」选择虚构 CSV，预检新增 3 条后确认导入。重复预检应显示 3 条重复，可直接关闭。
4. 切至「全部历史」，按项目「虚构断网验收」筛选，图表应能悬停，图例、图标和数字字体正常。浏览器开发者工具控制台运行 `await document.fonts.ready; [...document.fonts].map(f=>[f.family,f.status])` 可检查本地数字字体加载情况。选东京时区并自定义 2026-04-13 当天，应显示 170 Token、总量覆盖 3/3。保持该日期切到上海应为空，切回东京应恢复。
5. 按剔除缓存读取排序，展开入口并点入模型。预期净用量 45、覆盖 2/3。导出 JSON、CSV，各有三条记录，B 的缓存读取为空，C 是零。返回总览应保留筛选，再通过「数据接入 → 停止本地服务」退出。
6. 再次双击启动，选全部历史，确认仍为 3 条、170 Token。记录启停、筛选、导出及重启结果，保留两份导出作凭据。以上为待执行步骤；现有开发机回环测试的记录见[本地验收](本地验收-1.7-preview.md)。

