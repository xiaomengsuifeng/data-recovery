# 只读虚拟卷的直接扫描验收

日期：2026-09-14。本轮补齐桌面“选择磁盘”入口的设备读取验收，新增可重复执行的只读 VHD 验证工具。它使用之前实际删除留下的冻结材料，创建独立副本，再比较直接卷访问与同一 raw 镜像的扫描、导出和原件验证结果。

## 新增工具与修复

- `tools/windows/Invoke-LiveVolumeValidation.ps1` 只接受完整合成样本目录，不接受已有磁盘号或盘符。预检 Python、Qt、TSK 和所有样本原件/阶段摘要后，将选定 raw 阶段与校验过的 fixed VHD footer 复制到全新目录；固定限定 128 MiB。
- 仅以 `Mount-DiskImage -Access ReadOnly -NoDriveLetter` 挂载新副本。检查文件路径、摘要、新磁盘身份、文件支持的虚拟盘类型、只读属性、分区偏移、卷标及合成标记后，才将该卷交给桌面扫描。没有格式化、删除文件或清空回收站操作。
- `tools/live_volume_validation.py` 驱动实际 Qt 选择卷、预览、导出、重开；检查镜像专用的 PNG/日志选项在卷模式禁用。同盘工作目录和导出目标必须在创建输出前被拒绝。
- `finally` 只卸载本轮副本，核对输入 raw、原 VHD、副本的完整 SHA-256，以及操作前后的磁盘、分区清单。卸载后再次使用已保存会话尝试预览和导出，两者均应拒绝，且不创建恢复目录。
- 修复 `New-RecoveryFixture.ps1` 重新挂载时对未分配盘符的判断：Storage 可能返回 NUL 字符，其字符串并非空值；现在检查是否为实际字母。新增单独函数测试，无磁盘写入。
- 新验收脚本完整保留原生进程的标准错误，再检查退出码，避免 Windows PowerShell 5.1 的 `ErrorActionPreference=Stop` 在第一行错误信息处截断诊断。

只读挂载行为依据 [Microsoft Mount-DiskImage](https://learn.microsoft.com/en-us/powershell/module/storage/mount-diskimage?view=windowsserver2025-ps)，设备读取接口边界参见 [Microsoft CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)。挂载动作仅属于验收工具，产品恢复引擎不会自行挂载、修复或写入源卷。

## 已执行的开发环境检查

实际原生 Qt 运行记录：`artifacts/live-volume-acceptance/live-validation-f9171931bcc94e1083585e6e8e8e9b6f/`。输入为冻结基本样本 `31fb9b1c3c98413e84a78627928bd0ee` 的 `after-direct-delete` 阶段。

直接卷与 raw 镜像的候选列表、每份导出的大小、SHA-256 和路径声明一致。4 个直接删除目标的内容及原路径均匹配原件；文本和 PNG 预览、选择导出、重开后再次预览均完成。同盘扫描、同盘保存、卸载后预览和保存均被拒绝，禁止位置没有创建输出。输入 raw、原 VHD、副本摘要未变，副本已卸载，已有磁盘和分区清单未变。

卷恢复报告仍为 `source_verification: live_volume_identity_only`。产品只核对卷身份，不能从中声称一般在线卷的内容没有变化；本次副本的字节不变由独立验收工具在卸载后重新计算 SHA-256 证明。恢复引擎仍只接收所选卷；原件和冻结镜像只用于独立对照。

早期失败的探测及脚本日志保留在同一材料根目录。`probe-63aca38521144dcfaf2ba8590d27e023/` 记录 NUL 盘符问题，`live-validation-a1f37b92a57a4b2da76cb759b912214e/` 记录验收 guard 缺少 schema 的修正前失败，均没有改变输入或遗留挂载。`permission-probe.json` 的尝试发生在卷已卸载后，状态为 `not_run_volume_detached`，不作为非管理员权限拒绝的通过证据。

## 全量测试与便携包复验

管理员完整套件运行 221 项，全部通过，无失败、错误或跳过。新增用例覆盖固定 VHD footer/长度校验、原件变化及已有目标拒绝、只读 guard、镜像与卷结果不一致、盘符 NUL 字符和原生标准错误日志。记录位于 `artifacts/live-volume-acceptance/tests/admin-tests.json` 和同目录日志。

新包实际运行记录为 `artifacts/live-volume-acceptance/package-after-empty-recycle-bin/live-validation-12f451369bbc48e496fe62f25a2c7c1f/`。从包内 `validation/Invoke-LiveVolumeValidation.ps1` 启动，未覆盖 Python/TSK 路径；报告确认核心和桌面模块均来自新包，Qt 平台为 `windows`。

| 执行对象与阶段 | 删除目标 | 内容匹配 | 原路径匹配 | 与同阶段普通镜像扫描一致 |
| --- | ---: | ---: | ---: | --- |
| 开发环境，直接删除 | 4 | 4 | 4 | 是 |
| 新便携包，清空回收站后 | 8 | 4 | 4 | 是 |

新包同样完成文本/图片预览、导出、会话重开，以及同盘扫描/保存和卸载后访问的拒绝检查。两轮副本均已卸载，输入 raw、原 VHD、副本的摘要未变，前后已有磁盘和分区清单一致。清空回收站阶段的 4/8 是普通元数据扫描结果；卷模式禁用 PNG/旧日志扫描，不能直接与启用这两项功能的镜像 6/8 结果混比。本轮没有修改恢复核心。

在新包测试卷实际挂载期间，普通用户探测结果为 `is_admin: false`、`status: read_allowed`，能够读取该只读虚拟卷。记录位于 `package-after-empty-recycle-bin/permission.json`；这没有证明真实物理盘的权限行为，也不构成权限不足提示或 UAC 取消的验收。

最终用普通用户重新读取两轮的 16 份实际导出文件（每轮 raw 与卷各 4 份），核对字节长度、SHA-256、路径及独立原件；同时复核包内源码、验证工具、逐文件清单和 ZIP 完整性。综合记录为 `artifacts/live-volume-acceptance/delivery-checks.json`，其中 `full_physical_media_acceptance` 为 `false`。原生窗口截图保存在各轮 `material/`，已检查新包图片预览和保存结果页面。

本地交付包：`dist/ShiHui-0.2.0-volume-validation-windows-x64.zip`，共 118,947,587 字节，SHA-256 为 `da6cbf79c130ae435fad8f5bceb630ea68ffb03c4f77a925cb29942566ffae0c`。398 个文件清单校验、147 个 PE 检查和实际 Qt/TSK 启动检查均通过；证据为 `package-startup.log`、`package-pe.json` 及上述综合记录。本轮未发布新 Release。

## 使用方法

在管理员 PowerShell 中，向已经存在的父目录写入新的验收材料：

```powershell
.\tools\windows\Invoke-LiveVolumeValidation.ps1 -Fixture C:\RecoveryFixtures\ntfs-fixture-ID -OutputParent D:\Validation -Stage after-direct-delete -TskBin C:\Tools\tsk\bin
```

`-Stage` 可选 `after-direct-delete` 或 `after-empty-recycle-bin`。便携包中使用 `.\validation\Invoke-LiveVolumeValidation.ps1`，无需指定 Python/TSK 路径。结果位于新建的 `live-validation-<ID>/`，失败材料也保留。不要同时挂载同一合成卷的其他副本。

这里退出 0 表示设备入口与镜像结果一致、原生流程和保护检查完成，**不表示所有删除目标都已恢复**。恢复匹配数在 `material/live-volume.json`、`image-verification.json`、`volume-verification.json` 中；副本卸载及源摘要检查在 `workflow.json` 中。源卷断开后的检查见 `material/disconnected.json`。

## 尚未覆盖

本轮是 Windows 原生设备路径上的只读虚拟 NTFS 卷测试，不代表真实硬盘/SSD、写入中的系统卷、TRIM、硬件断连或不同控制器。测试中“不同盘”依据 Windows 暴露的磁盘身份；文件支持的虚拟盘与宿主物理介质应分开理解。真实物理盘隔离、扫描过程中断连、UAC 取消和不同 DPI 仍需单独验收。原件已经丢失的日志小文件没有因此新增恢复能力，镜像 PNG/旧日志功能的既有边界保持不变。
