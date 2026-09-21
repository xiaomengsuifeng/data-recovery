# 0.2.1rc1 软件收尾与交付边界

日期：2026-09-21。用户确认暂时没有专用外接测试盘，先完成软件部分。本轮以 [首版产品范围](../product-brief.md) 中的 Windows NTFS 桌面恢复为边界。exFAT/JPEG 专项、坏盘采集和断点续扫保留为后续扩展。

## 本轮完成的代码

- Windows 卷和目标盘查询共用有超时、输出上限、取消与子进程回收的执行器。关闭 PowerShell 进度输出，避免 Storage 正常进度被当成错误；无效 JSON、身份字段、权限与设备读取错误有明确提示。
- 管理员启动改用 `ShellExecuteExW`，区分取消授权与启动失败；失败时保留窗口和当前选择，成功重启时传递扫描记录及工作目录。接口依据 [Microsoft ShellExecuteExW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shellexecuteexw)。
- 导出时源卷断开或身份改变，保留已写出的部分字节、停止后续文件并记录来源错误。空间查询失败会留下报告；目标无法写入报告时，尝试存到已经核对过的扫描记录目录，两处均失败时明确报错。
- 结果页展示失败原因、未处理数量及报告备用位置；存在部分结果、失败或跳过时不显示“保存完成”。任务失败可以重试，关闭窗口会等待取消与子进程清理。
- 根据屏幕逻辑尺寸调整初始窗口和侧栏；缩小时页面可滚动，导航和取消保持可见。新增 `run_desktop_acceptance.py` 验证 100%、125%、150%、200% 原生 Qt 缩放下的实际 TSK 扫描、文本/图片预览、导出、原件核验与重开。
- 候选包附完整文档及验收工具，使用说明中的相对链接能在解压目录中打开。新增 Windows/Linux、Python 3.11/3.13 的单元测试 CI 配置；远端 CI 结果需在提交上传后产生。

## 验收方式

自动回归涵盖取消进程回收、拒绝权限、断连后的部分输出、目标满盘、报告写入失败、UAC 取消后的界面状态及真实窗口操作。故障注入用于确定错误分支行为，不能代替硬件拔盘或安全桌面的真实 UAC 操作。

2026-09-21 的 Windows 管理员最终全量测试为 **238 项全部通过，0 失败、0 错误、0 跳过**。运行日志、逐项结果及进程退出码保存在下述材料目录的 `tests/final/` 中；包含取消与目标盘同时断开仍能保留报告的新增回归。最终包的逐文件校验、PE 检查、四档界面结果和只读卷回归分别保留机器可读记录，由 `delivery-checks.json` 汇总。

实际调用新版管理员启动函数，从普通用户 Qt 进程启动了只写自身验收结果的管理员子进程，中文含空格参数完整保留，记录在 `elevation-parent.json` 和 `elevation-child.json`。这验证了实际启动接口及参数传递；真实 UAC 取消仍未操作，取消分支单独使用故障注入验证。

原生界面测试使用之前独立保存原件的固定样本 `31fb9b1c3c98413e84a78627928bd0ee` 的直接删除镜像。恢复引擎只读取镜像，原件清单只交给验证器；每轮分别统计 4 个目标的内容与原路径。脚本需要新的输出目录，示例：

```powershell
.\.venv\Scripts\python.exe -B tools\run_desktop_acceptance.py --image C:\Fixtures\after-direct-delete.img --manifest C:\Fixtures\after-direct-delete.manifest.json --output D:\Validation\dpi-200 --tsk-bin C:\Tools\tsk\bin --scale 2
```

便携包使用 `runtime\python.exe -B validation\run_desktop_acceptance.py --installed-runtime`，后续参数相同。它只修改自身 Qt 进程的缩放，不改变系统显示设置。`desktop-acceptance.json` 记录实际导入模块、平台、缩放、窗口尺寸及独立原件结果。

本轮材料根目录为 `artifacts/software-completion-20260921/`。完整测试记录在 `tests/`，包内多档缩放记录在 `package-dpi-*/`，直接卷回归在 `package-live/`。便携包为 `ShiHui-0.2.1rc1-windows-x64.zip`；最终摘要和校验值单独保存在 `delivery-checks.json`，避免把包的 SHA-256 写回包内形成自引用。

## 交付状态

这是 NTFS 首版范围内的软件候选版。既有恢复能力边界保持不变：镜像支持元数据、可选 PNG 和部分旧日志恢复；直接卷支持元数据恢复。覆盖或清理后缺少完整证据的文件仍可无法恢复，不能把片段或候选当成完整原件。

真实硬盘/SSD/移动介质、读取中物理拔盘、真实 UAC 取消及跨显示器 DPI 切换仍是外部环境验收事项。用户已选择暂不提供专用介质，本轮不会把这些项目标为通过，也不会为凑齐验收而操作现有 C/D 磁盘的数据。
