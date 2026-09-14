# Windows 11 真实删除验收

验收日期：2026-09-14。已实际启动管理员进程，在本轮新建的 128 MiB fixed VHD 中生成合成文件，执行直接删除、Windows Shell 移入回收站及仅清空测试卷回收站，再用真实 TSK 对照独立保存的删除前原件。

本文保留仅元数据恢复的基线结果。后续 PNG 内容扫描对相同冻结材料的改善见[下一里程碑](06-png-content-recovery.md)。

**结论：两条即时恢复路径通过，组合场景未全通过，整体状态保持 `incomplete`。** 后续回收站操作复用了较早删除文件的 NTFS 记录，当前元数据恢复未在后续阶段重新找回这 4 个目标。没有通过调整目标清单、降低分母或向扫描后端提供原件信息来改写结论。

## 实际结果

| 阶段 | 删除目标 | 内容一致 | 原名及完整路径一致 | 状态 |
| --- | ---: | ---: | ---: | --- |
| 删除前 | 0 | 0 | 0 | baseline；匹配率为空值 |
| 直接删除后 | 4 | 4 | 4 | passed；包含中文名称及整目录删除 |
| 另 4 个文件移入回收站后 | 4 | 0 | 0 | incomplete；早先目标记录已被复用 |
| 清空测试卷回收站后 | 8 | 4 | 4 | incomplete；4 个回收站目标全部匹配，早先 4 个仍未找回 |

9 个原件中有 1 个保留对照，不计入删除目标。8 个不同删除目标跨阶段形成 16 次验证，本轮内容及路径匹配均为 8 次，不能表述为“8 个文件始终全部恢复”。在最后阶段恢复出的回收站目标包含中文名称、不同目录同名文件及 PNG。

四阶段恢复报告中的来源复核均为 `unchanged`。扫描、导出及验证实际重新读取镜像和文件字节。管理员生成的 8 个导出文件随后由同一普通用户按报告路径逐个打开并重新核对 SHA-256；另在独立测试输出中验证了普通用户创建和删除文件的权限。

测试 VHD 最终全部卸载，运行前后的已有磁盘身份、大小、分区类型及系统/启动标志清单一致。实验只格式化本轮新建并核对身份的 VHD，没有将已有物理磁盘或用户文件作为格式化、删除目标。

## 验收中发现并修复的问题

1. **DiskPart 脚本编码。** UTF-16 命令文件在本机只输出启动信息，退出 0 却未创建 VHD。只读 `list vdisk` 对照探针复现后，改为系统 ANSI 代码页、无 BOM、完整换行，并拒绝不能表示的路径；还增加实际文件存在检查。保留连续 DiskPart 调用至少 15 秒的间隔。[Microsoft 脚本要求](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/diskpart-scripts-and-examples)
2. **虚拟磁盘类型检查。** `Get-Disk.BusType` 是显示字符串，不能直接转换为整数。改用底层 CIM 的 UInt16 值验证 File Backed Virtual，保留系统盘、物理盘、容量和身份拒绝条件。[MSFT_Disk 定义](https://learn.microsoft.com/en-us/windows-hardware/drivers/storage/msft-disk)
3. **样本路径父目录检查。** `.Parent`、`.Directory` 返回的普通 `DirectoryInfo` 没有 `Get-Item` 添加的 `PSIsContainer` 属性。改用实际对象类型继续遍历，路径范围和重解析点检查保持执行。
4. **提权后的导出权限。** Python 的 Windows `mode=0o700` 使用 OWNER RIGHTS；管理员进程创建的目录可能归 Administrators 所有，导致普通用户打不开。现在用 Win32 创建仅授权当前用户 SID、SYSTEM 和 Administrators 的目录，导出子目录继承同一 ACL。[CPython 3.13.12 对应实现](https://github.com/python/cpython/blob/v3.13.12/Modules/posixmodule.c#L5273)
5. **Windows TSK 中文文本编码。** 本机 TSK 通过文本管道输出 CP936，原先强制 UTF-8 会使中文扫描失败。按 Windows 原生代码页严格解码，Unix 仍使用 UTF-8；不逐条猜编码。真实样本新增直接删除 `中文.txt`，修复后通过。依据为锁定的 TSK 4.15.0 源码中 `fls.cpp` 的 `setlocale` 和 `tsk_printf.c` 的宽字符输出，以及本机实际输出字节。

新增 11 项回归测试，最终 **142 项自动测试全部通过，无失败、无错误、无跳过**。管理员环境实际执行了此前普通会话跳过的两个符号链接用例。代码修复后还重新运行原生 Qt + TSK 的 NIST 镜像回归，扫描、预览、保存、重新打开和参考区域字节核对通过。

## 记录复用的证据

直接删除阶段的 MFT 记录 41、42、45、46 对应四个直接删除目标。移入回收站阶段，它们已分别用于 `$RECYCLE.BIN`、当前用户回收站目录及两个 `$I` 索引；TSK 的完整目录列表显示这些新对象处于已分配状态。当前扫描会拒绝将已重分配记录当作旧文件导出。这说明本轮缺失与元数据复用有关，不证明所有原内容字节都已消失，也不证明其他方法必然能够恢复。

## 本地证据与复现

最终材料与报告位于 `artifacts/admin-acceptance-20260914-154759/`，这些含镜像和合成文件的本地证据不提交到 Git：

- `admin-result.json`、`console.log`：管理员真实流程，退出码 1、状态 incomplete。
- `recovery-validation-15332e1966b449bbbdd0f94febd00ab6/fixtures/ntfs-fixture-31fb9b1c3c98413e84a78627928bd0ee/`：9 个原件、四张镜像、清单、回收站证据及操作日志。
- 同一工作目录的 `results/`：分阶段扫描、实际导出、逐目标校验和 `fixture-validation.json`。
- `acceptance-review.json`：普通用户逐文件读取、哈希核对、来源和卸载状态的复核。
- `metadata-reuse.json` 与 `*-all-records.body`：完整 TSK 列表中相关 MFT 记录的前后对照，原始 body 字节为 CP936。
- `admin-tests.json`、`admin-tests.log`：最终 142 项测试，无跳过。
- `disks-before.json`、`disks-after.json`、`vhd-attachments.json`：已有磁盘清单及测试 VHD 卸载状态。
- `desktop-regression/`：本轮原生 Qt 桌面回归截图及报告。

前三轮脚本失败的材料分别保留在 `admin-acceptance-20260914-152301/`、`152946/`、`153251/`；首次完整实验及权限问题诊断位于 `153451/`。每次失败后均检查卸载状态，不复用失败目录继续生成。

修复后的本地包使用 `dist/ShiHui-0.2.0-windows-acceptance-x64` 名称，构建与包内复验记录保存到最终证据目录。原始 0.2.0 Release 和早先 fixture-validation 包未包含这些修复。本轮没有发布新版或推送提交。[运行方法](../windows-testing.md)

本地包已核对 394 个清单文件、147 个 PE 文件位数与依赖，包内 Python 3.13.12、Qt 和 TSK 启动通过。使用包内运行时重新验收本轮真实镜像，内容和路径匹配数同为 0、4、0、4，仍正确退出 1、报告 `incomplete`。ZIP 为 118,911,392 字节，SHA-256 为 `ff538008c1d2ac2929d2c5d9067ea836010eb7063d5c5976cc45b5356fe2e62b`，详细结果保存在 `package-checks.json` 和 `package-fixture-validation/`。

## 剩余范围

本轮是单台 Windows 11 10.0.26200、PowerShell 5.1.26100.9444、CP936、逻辑 VHD 的合成 TXT/PNG 实验。未覆盖原始卷恢复入口、应用自身 UAC 取消、设备拔出、不同 DPI、真实 HDD/SSD/U 盘、TRIM、坏盘、加密/压缩文件、其他语言代码页和竞品对照。组合场景的不足需要继续研究记录残留或内容扫描，不能用本轮两个即时恢复结果宣称普遍恢复率。
