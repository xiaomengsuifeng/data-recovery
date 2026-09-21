# Windows 11 镜像流程验证

验证日期：2026-09-14。接续 0.2.0 桌面测试版，在 Windows 11 普通用户环境补齐源码运行证据。镜像模式已通过，本阶段仍未完成需要管理员权限的原始卷与真实删除验收。

## 本轮改动

- 新增 `tools/validate_windows.py`，统一执行单元测试、TSK 启动、Windows PowerShell 语法检查、只读卷枚举及实际 Qt 镜像演示，保存逐步日志和 JSON 汇总。
- 将“已有目标目录”和“符号链接目标”拆分为独立用例。普通目录防覆盖始终测试；仅 Windows 错误 1314 导致的符号链接权限不足会明确跳过，不掩盖其他错误。
- 为验证工具补充正常输出、中文日志、失败退出、超时、后代进程清理、缺失程序、已有证据保护，以及测试结果统计的回归测试。Windows 超时时结束当前检查的进程树，避免虚拟环境启动器留下占用日志的子进程。
- 桌面演示报告新增实际操作系统、Qt 平台和 Windows 镜像验证状态，保留完整 Windows 验收尚未完成的标记。

## 执行结果

主机为 Windows 11 x64，系统版本 `10.0.26200`，开发环境为 Python 3.13.2、PySide6-Essentials / Shiboken 6.8.3、TSK 4.15.0。两个 Python wheel 和 TSK 归档的长度及 SHA-256 均与 `tools/runtime-lock.json` 一致。

| 检查 | 实际结果 | 范围 |
| --- | --- | --- |
| 自动测试 | 108 项中 106 项通过、2 项跳过；无失败或错误 | Qt 用例全部执行；跳过项为权限不足的符号链接测试 |
| TSK CLI | `fls`、`icat` 4.15.0 启动与 NIST 扫描/导出通过 | 独立 Windows 可执行程序 |
| 原生 Qt 桌面 | 来源识别、扫描、搜索、预览、选择、保存、重开记录通过 | `qt_platform=windows`，输出路径含中文及空格 |
| 恢复内容 | `Bunda.txt` 4,296 字节，与固定参考摘要一致 | NIST 删除后镜像指定区域，不是独立删除前原件 |
| 卷枚举 | 识别 2 个 NTFS 卷，分属 2 块磁盘 | 只读 Storage 查询，未打开原始卷 |
| 位置核对 | 同盘目标拒绝 2 次、不同盘目标接受 2 次 | 对已有盘符目录做身份核对，不执行恢复或写入 |
| PowerShell 脚本 | Windows PowerShell 5.1.26100.9444 解析通过 | 未执行 VHD、格式化、删除或 Shell 回收接口 |
| 发布便携包 | ZIP 摘要与发布值一致，包内 391 个文件匹配；Qt/TSK 启动通过 | 使用包内 Python 3.13.12，不依赖开发虚拟环境 |
| 包内启动器 | `pythonw.exe` 执行 `launch.pyw`，原生窗口成功显示并截图 | 确认应用模块从包内 `src/` 加载；未触发 UAC |

NIST 镜像 SHA-256 为 `c863ccad01804b840a6dfa623a94996ca876e15ded41c6c0d8ae148620eb6493`。导出文件 SHA-256 为 `be9f9a4f99b5ce961d5c2759fff20543d0ffbedbd27be0c5157174bb46be8b85`。来源核对和验证器均实际重新读取文件字节。[样本来源与解释](../../tests/integration/fixture-source.md)

符号链接跳过项是 `test_destination_symlink_is_not_reused` 与 `test_file_and_directory_symlinks_are_rejected`。当前会话没有相应权限；这是测试覆盖边界，不记为通过。[Python 的 Windows 符号链接权限说明](https://docs.python.org/3/library/os.html#os.symlink)

本地原始证据位于 `artifacts/Windows 验证 02/`：`environment.json`、`tests.json`、`validation.json`、各步骤日志和 `desktop/` 中的截图、扫描/恢复/校验报告。`artifacts/` 不提交到 Git；复现方法见[开发说明](../development.md)。

便携包检查单独记录在 `artifacts/package-startup-windows.log` 与 `artifacts/package-native-startup/`。本次检查的是已发布的 `ShiHui-0.2.0-windows-x64.zip`，118,891,506 字节，SHA-256 为 `1bd2680a90f83ce15e1245a53d1ec0e85508c36498b1d06c847e7d128b3d9305`。使用其自带 `check_package.py` 检查清单、Qt 和 TSK，然后通过包内 `pythonw.exe` 执行启动器，Qt 定时器在截图后退出窗口。没有修改包内文件；未把开发机的源码或虚拟环境注入包内应用。统一验证入口的汇总仅涵盖源码镜像流程，不自动包含此独立包检查。

## 后续验收

当前非管理员会话没有执行 UAC、隔离 VHD 生成、直接删除、Windows Shell 清空回收站或原始卷读取。接下来需要在管理员测试会话中按[隔离样本使用方法](../windows-testing.md)运行 New-RecoveryFixture.ps1，保存删除前原件，并按阶段清单独立核对导出内容与原名/目录。

还需验证原始设备断开、真实源/目标隔离、CMD/快捷方式双击入口及不同 DPI、真实 HDD/SSD/移动介质。Windows 镜像流程和包内启动器通过不代表这些项目已通过，也不产生恢复率或竞品优势结论。
