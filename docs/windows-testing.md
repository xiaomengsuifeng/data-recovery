# 在 Windows 11 生成并验证 NTFS 恢复样本

状态：2026-09-14 已在管理员 Windows 11 会话中实际运行 VHD 生成、直接删除、Windows Shell 回收与清空，并用真实 TSK 对照删除前原件。直接删除阶段为 4/4，清空后的回收站目标为 4/4；较早直接删除的记录被后续操作复用，组合阶段为 4/8，整轮保持 `incomplete`。142 项自动测试全部通过，无跳过。材料只有合成 TXT 和 PNG；Office、真实拍摄照片、物理介质和系统盘行为仍需要后续验证。

请使用当前源码或本轮重新构建的便携包。原始 0.2.0 发布包未包含新增入口、脚本兼容性、中文解码和导出目录权限修复；早先构建的 fixture-validation 本地包也需要更新。

## 脚本会做什么

[`New-RecoveryFixture.ps1`](../tools/windows/New-RecoveryFixture.ps1) 在指定父目录下建立全新的 `ntfs-fixture-<随机ID>` 子目录，然后：

1. 用 Windows 自带 `diskpart` 创建一个 **128 MiB、fixed 类型的新 VHD**。
2. 通过 `Get-DiskImage → Get-Disk` 确认它是新出现、未初始化、非系统/启动盘的虚拟磁盘，只在该盘创建 NTFS 分区。盘符从尚未使用的 D–Z 中选取。
3. 创建 9 个合成原件，含小文件、大于常见驻留数据尺寸的文本、PNG、中文名称、不同目录同名文件、嵌套目录和 1 个保留对照。原件副本与 SHA-256 清单保存在 VHD 外。
4. 对 4 个目标执行绕过回收站的文件/目录删除；其逻辑效果用于代表 Shift+Delete，不伪称脚本发送了真实快捷键。
5. 对另 4 个目标调用 Windows Shell 回收接口。随后检查测试卷内当前用户的 `$I` 原路径/大小和对应 `$R` 内容哈希，确认文件确实进入回收站。
6. **仅清空该测试卷的回收站**，再确认对应索引与内容已不再分配。
7. 每阶段刷新并卸载 VHD，导出 raw 镜像和目标清单；最后保持 VHD 卸载，不自动删除任何测试输出。

脚本没有用于指定已有盘、已有卷、已有 VHD 或任意待删除目录的参数。不运行 `clean`，不修改全局回收站、TRIM 或磁盘自动挂载设置，不访问真实用户文件来生成样本。宿主机上发生的普通写入仅用于脚本工作目录、系统运行及 Windows 工具自身行为。

## 自动生成并验收

`Invoke-RecoveryValidation.ps1` 先检查 Python 命令及 TSK 是否可用，再调用上述生成脚本，最后对完整样本逐阶段扫描、导出并核对原件。它只使用本次新生成的目录。使用当前源码时，先按开发说明安装依赖，并在 **64 位管理员 Windows PowerShell 5.1** 中执行：

```powershell
New-Item -ItemType Directory -Path C:\RecoveryFixtures
powershell.exe -NoProfile -STA -File .\tools\windows\Invoke-RecoveryValidation.ps1 -OutputParent C:\RecoveryFixtures -TskBin C:\Tools\sleuthkit-4.15.0-win32\bin
```

父目录已存在时跳过第一行。默认 Python 是项目的 `.venv\Scripts\python.exe`，也可通过 `-Python` 指定已安装本项目的解释器。当前源码重新构建的便携包可直接使用包内 Python 和 TSK，在包根目录执行：

```powershell
powershell.exe -NoProfile -STA -File .\validation\Invoke-RecoveryValidation.ps1 -OutputParent C:\RecoveryFixtures
```

每轮创建 `recovery-validation-<ID>/`，其中 `fixtures/ntfs-fixture-<ID>/` 保留镜像和删除前原件，`results/` 保留恢复及验收结果，`workflow.json` 记录整轮状态。生成失败时保留材料和日志，停止后续验收。此串联脚本不会自动提权，必须由管理员会话启动。

## 自动验收已有样本

有完整的 `ntfs-fixture-<ID>` 目录时，可以在普通用户会话运行；Windows、macOS 和 Linux 均使用同一个命令。它只读取普通镜像文件，不需要挂载 VHD。Windows 源码示例：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m recovery_core validate-fixture C:\RecoveryFixtures\ntfs-fixture-ID --output C:\RecoveryFixtures\verified-01 --tsk-bin C:\Tools\sleuthkit-4.15.0-win32\bin
```

便携包把解释器换为 `.\runtime\python.exe`，工具目录换为 `.\vendor\tsk\bin`。`--output` 必须是样本目录之外、尚不存在的新目录，且其父目录已存在。`--timeout` 可调整每个 TSK 进程的超时秒数，默认 120。

验收先检查生成状态、样本 ID、9 个原件副本的实际长度与 SHA-256、四张镜像的摘要与 NTFS 几何参数，以及每阶段删除目标是否完整。清单缺项、保留对照混入目标、重复目标和路径越界都会在扫描前拒绝。校验后冻结参考清单，恢复后端只接收该阶段镜像与几何参数，不接收原件、目标清单或 `recycle-evidence.json`；缺少 `$I` 时不能借助真值补出原名。

输出包括：

- `validated-inputs.json`：本轮核对过的镜像身份、原件摘要和阶段目标。
- 每阶段目录中的 `session/session.json`、`recovered/recovery.json` 和实际导出文件，以及 `reference.manifest.json`、`verification.json`。
- `fixture-validation.json`：各阶段状态、候选和导出数量、内容匹配数、正确名称/目录/路径数，以及总体状态。

四阶段的目标数分别为 **0、4、4、8**。原始阶段记为 `baseline`，零目标的匹配率为空值。`unique_deleted_targets` 是 8 个不同删除目标；`planned_target_observations` 是跨阶段合计的 16 次验证，不代表 16 个独立文件。内容一致但原名/路径不正确时仍记为 `incomplete`。扫描失败会记录原因并继续后续阶段；取消保留已完成的结果，剩余阶段记为 `not_run`。

退出码：`0` 表示所有删除阶段的目标内容和路径均匹配且来源复核通过；`1` 表示未全匹配；`2` 表示材料无效或运行失败；`130` 表示取消。生成器的 `complete` 只表示材料生成完毕，验收器的 `passed` 才表示本轮已登记目标通过核对，两者都不代表其他介质上的普遍恢复率。

## 仅生成材料

使用当前源码重新构建的便携包时，完整解压后在管理员 PowerShell 中进入软件根目录，运行 `powershell.exe -NoProfile -STA -File .\validation\New-RecoveryFixture.ps1 -OutputParent C:\RecoveryFixtures`。先创建普通目录 `C:\RecoveryFixtures`，后续步骤与源码版本相同。生成镜像后可直接在拾回界面选择对应 `.img`，工作目录另选普通文件夹。

需要 Windows 11 的 **64 位 Windows PowerShell 5.1**、管理员身份、内置 Storage 模块和约 **1 GiB 可用空间**。不需要 Hyper-V 可选组件、WSL、Python、Office 或额外下载。

将项目复制到 Windows，例如 `C:\data-recovery`。在“以管理员身份运行”的 Windows PowerShell 中，准备一个本地普通目录，然后运行：

```powershell
New-Item -ItemType Directory -Path C:\RecoveryFixtures
powershell.exe -NoProfile -STA -File C:\data-recovery\tools\windows\New-RecoveryFixture.ps1 -OutputParent C:\RecoveryFixtures
```

若父目录已经存在，跳过 `New-Item`。每次脚本运行都会另建随机子目录，不会覆盖上一轮。父目录必须是本地目录，且目录及祖先不能为重解析点；UNC 网络路径、目录联接、符号链接会被拒绝。脚本不需要访问另一台电脑，也不会请求其远程凭据。

DiskPart 命令文件使用 Windows PowerShell 5.1 的系统 ANSI 代码页，无 BOM，并拒绝无法表示的路径。本机实测代码页为 936；遇到编码错误时改用 ASCII 父目录。UTF-16 命令文件在本机曾导致 DiskPart 返回 0 却不执行命令，因此脚本还检查 VHD 是否实际创建。

如果执行策略拦截脚本，先检查内容；需要时仅为该启动进程加入 `-ExecutionPolicy Bypass`，不必修改系统执行策略。脚本会在两次 `diskpart` 调用之间保留至少 15 秒间隔，因此开始时短暂停顿属于预期行为。[Microsoft diskpart 脚本说明](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/diskpart-scripts-and-examples)

运行期间不要把个人文件放入临时测试盘，不要在其中打开编辑器或资源管理器窗口，也不要手动改变盘符、卷标或虚拟盘附件状态。检查到身份、路径、哈希或回收站记录异常时，脚本停止，不回退到操作其他卷。

## 阶段输出

| 文件前缀 | 镜像内容 | 对应 `.manifest.json` 的删除目标 |
| --- | --- | --- |
| `before-delete` | 所有合成文件均存在 | 空数组；用于原始布局检查，不用于删除恢复分母 |
| `after-direct-delete` | 直接删除两个文件及含两个文件的目录 | 4 个直接删除目标 |
| `after-move-to-recycle-bin` | 上述 4 个已删除；另 4 个仍以 `$R` 内容存在于回收站 | 仍为 4 个直接删除目标；不能把回收站中仍分配的内容计为清空后恢复 |
| `after-empty-recycle-bin` | 4 个直接删除目标，加 4 个已经从测试卷回收站清空的目标 | 8 个删除目标 |

每个前缀对应一个 `.img` 和一个 `.manifest.json`。此外包含：

- `originals/` 与 `originals.manifest.json`：9 个删除前原件及完整清单，含保留对照；不应拿完整清单当某个阶段的删除目标分母。
- `stages.json`：镜像相对文件名、大小、SHA-256、分区起始扇区 `offset_sectors`、扇区大小和阶段目标数。
- `recycle-evidence.json`：清空前验证过的 `$I/$R` 对应关系，作为独立真值，不提供给恢复引擎扫描过程。
- `environment.json`：系统、PowerShell、进程架构、脚本哈希等；`events.jsonl` 与 `*.diskpart.log`：操作日志。
- `result.json`：只有 `status: complete` 才表示所有计划阶段材料生成完成，不表示任何恢复算法已经成功。
- `fixture.vhd`：最终状态的原始固定 VHD，已卸载；保留以便排查。

目标清单使用 CLI 约定：

```json
{
  "schema_version": 1,
  "stage": "after-direct-delete",
  "sector_size": 512,
  "offset_sectors": 2048,
  "files": [
    {
      "original_path": "Z:\\Samples\\direct\\small-note.txt",
      "size": 123,
      "sha256": "<actual SHA-256>",
      "scenario": "direct-file"
    }
  ]
}
```

示例中的盘符、大小、摘要及偏移只是说明，**实际必须读取生成文件**。每阶段的 manifest 只含当时已删除的目标。验证端比较路径时去掉 Windows 盘符并统一分隔符；TSK 路径通常从卷根开始，如 `/Samples/direct/small-note.txt`。

## 扩展样本与追加写入

默认 `basic` 配置保留上述 9 个原件、4 个阶段。当前源码及新本地包可以指定 `-Profile expanded`：生成 36 个原件，其中 34 个删除目标和 2 个保留对照。新增内容包含随机二进制和随机字母文本、0/1 字节边界、接近 4 KiB 的文件、长中文路径，以及改写、截短、重命名后的最终原件。文件大小不等于驻留属性类型，仍应检查实际文件记录。

`-WritePressureMiB 4` 在原四阶段之后，向本轮新建 VHD 写入 64 个 257 字节小文件及 4 个 1 MiB 文件，生成第五张 `after-additional-writes.img`。允许范围为 0～64；0 不追加阶段。参数表示实际追加的批量数据量，不能换算成删除内容覆盖百分比，Windows 自己选择分配位置。追加文件不计入删除目标，`write-pressure.json` 保存其路径、大小及 SHA-256。

管理员 PowerShell 中可直接生成并验收，`OutputParent` 需预先存在：

```powershell
.\tools\windows\Invoke-RecoveryValidation.ps1 -OutputParent C:\RecoveryFixtures -Profile expanded -WritePressureMiB 4 -DeepPng -DeepLog -TskBin C:\Tools\tsk\bin
```

便携包中使用 `.\validation\Invoke-RecoveryValidation.ps1`，省略 `-TskBin` 即采用包内工具。对已经生成的扩展材料继续用 `validate-fixture` 命令，无需管理员权限或重新挂载 VHD。

扩展五阶段目标数为 **0、26、26、34、34**，跨阶段观察共 120 次，独立删除目标仍为 34 个。验证器检查第五阶段与压力参数、工作量清单一致，并重新核对所有原件、镜像及各阶段目标；额外按 `by_scenario` 和 `by_size_bytes` 汇总实际匹配的目标，重复候选和片段不会增加完整匹配数。大小分组使用精确字节数。失败或取消的阶段不生成伪造的分组通过数。

本机两组独立扩展材料分别追加 4 MiB、32 MiB，清空回收站后均完整匹配 28/34，追加写入后均只有 2/34 的 PNG 内容匹配、原路径 0/34。结果均为 `incomplete`，不能以基本样本或追加前的结果推断覆盖后的恢复率。[扩展验收和小文件证据调查](milestones/08-expanded-fixture-validation.md)

## 为什么导出 `.img`，而不直接改名 `.vhd`

固定 VHD 的数据与动态/差分 VHD 的映射方式不同。本脚本只接受自己刚创建的 fixed VHD，检查末尾 512 字节 footer 的 `conectix` 标记、格式版本、磁盘类型 2、容量、固定盘数据偏移标记和 checksum。只有检查通过且文件长等于虚拟容量加 512 字节时，才复制前 128 MiB 为 raw `.img`；原 VHD 不被截断。动态 VHD/VHDX 不适用这个操作。[Microsoft VHD footer 实现](https://github.com/microsoft/azure-vhd-utils/blob/master/vhdcore/footer/footer.go)

`.img` 是**整盘镜像**，前面仍含分区表；NTFS 通常从非零偏移开始。TSK 原始镜像读取应使用 `stages.json` 对应的 `offset_sectors`，单位固定为 512 字节，不要把字节数当扇区数。不能以某次常见的 2048 替代实际记录。

## 将材料用于 Mac CLI

也可在 Windows 便携包内核对桌面导出的结果。进入软件根目录，将以下路径替换成实际值：

```powershell
.\runtime\python.exe -B -m recovery_core verify "D:\Results\recovered-实际编号" --manifest "C:\RecoveryFixtures\ntfs-fixture-实际编号\after-empty-recycle-bin.manifest.json" --output "D:\Results\verification.json"
```

`--output` 要求新文件。验证器重新读取导出字节，与删除前保存的原件摘要对照，不依赖扫描报告声称的哈希。

当前源码或含 PNG 功能的便携包可对已有四阶段材料启用深度扫描，无需重新挂载、删除或生成 VHD：

```powershell
.\runtime\python.exe -B -m recovery_core validate-fixture "C:\RecoveryFixtures\ntfs-fixture-实际编号" --output "D:\Results\png-validation-新编号" --deep-png --tsk-bin .\vendor\tsk\bin
```

未加 `--deep-png` 时保留元数据恢复基线。PNG 候选原名与路径未知，不能因内容匹配就计为路径恢复；重复候选仍按目标一对一计数。本机相同冻结材料的最终内容匹配由 4/8 增至 5/8，路径仍为 4/8，状态为 `incomplete`，详见[PNG 内容扫描验收](milestones/06-png-content-recovery.md)。原始 0.2.0 Release 尚无此选项。

当前源码或包含旧日志功能的本地包还可增加 `--deep-log`：

```powershell
.\runtime\python.exe -B -m recovery_core validate-fixture "C:\RecoveryFixtures\ntfs-fixture-实际编号" --output "D:\Results\log-validation-新编号" --deep-png --deep-log --tsk-bin .\vendor\tsk\bin
```

两组实际 Windows 冻结样本均得到内容 0、4、2、6，原路径 0、4、2、6，最终 `incomplete` 并返回 1。片段保存后仍为 `partial`，验证器不会将其计入完整文件匹配。阶段的 `ntfs_log` 报告保留被跳过的历史记录及原因；这里只支持部分日志格式。[旧日志恢复验收与限制](milestones/07-ntfs-log-recovery.md)

把完整的 `ntfs-fixture-<ID>` 子目录复制回 Mac。先核对 `.img` 的 SHA-256 与 `stages.json`；恢复引擎只输入故障后的 `.img`，原件和清单由验证阶段使用。

在项目根目录、已经配置好 CLI 依赖的环境中，按下列流程执行。将示例路径和偏移替换成该轮材料的实际值；session 与 destination 都选择尚不存在的新目录：

```sh
python -m recovery_core scan /path/to/ntfs-fixture-ID/after-direct-delete.img --output /path/to/sessions/direct-01 --offset 2048
python -m recovery_core recover /path/to/sessions/direct-01 --destination /path/to/recovered/direct-01
python -m recovery_core verify /path/to/recovered/direct-01 --manifest /path/to/ntfs-fixture-ID/after-direct-delete.manifest.json
```

然后对 `after-empty-recycle-bin.img` 使用新的 session 和 destination，以及它自己的 manifest。内容原样一致、原文件名正确、原目录正确、错误 `$I/$R` 关联分别评估；仅输出 `$R...` 名称不能算原名恢复。流程成功退出也不能替代逐目标内容校验。

## 验收“选择磁盘”直接扫描

`Invoke-LiveVolumeValidation.ps1` 对已完成的合成样本进行另一类检查：将选定阶段复制为新 fixed VHD，以只读方式挂载副本，再用原生 Qt 的磁盘入口扫描、预览、保存和重开。不会格式化或删除样本，也不接受已有磁盘号/盘符。

```powershell
.\tools\windows\Invoke-LiveVolumeValidation.ps1 -Fixture C:\RecoveryFixtures\ntfs-fixture-ID -OutputParent D:\Validation -Stage after-direct-delete -TskBin C:\Tools\tsk\bin
```

需在管理员 PowerShell 运行，两个路径均替换为实际存在的目录。`-Stage` 还可选 `after-empty-recycle-bin`。便携包中入口是 `.\validation\Invoke-LiveVolumeValidation.ps1`，可省略 Python 和 TSK 参数。

工具核对直接卷与 raw 镜像候选/导出的对应关系，并按独立原件统计恢复结果；同盘扫描、同盘导出及卸载后访问必须被拒绝。最后卸载副本，复核输入和副本摘要、已有磁盘和分区清单。`workflow.json` 的 `passed` 表示这些流程一致，不等于删除目标全部恢复。[证据、结果和仍需验证的物理介质范围](milestones/09-live-volume-validation.md)

## 失败时如何处理

脚本出错会保留已有材料和 `status: failed`，并尝试只卸载本轮 VHD。不要执行“清空回收站”桌面菜单来补做步骤，也不要修改脚本把根路径改成空字符串。

若日志表示未找到正确的 `$I/$R`、数量不符或 Shell 回收失败，该轮不构成清空回收站测试成功。Windows 回收站配置可能让 Shell 直接删除合成文件；脚本会通过缺失的对应证据检测并停止，不会把它写成真实回收站阶段。修正测试环境后新开一轮，保留旧日志；不应为了成功而禁用上述检查。

若最终提示无法卸载 VHD，关闭仅属于临时测试盘的窗口，然后在“磁盘管理”里核对 VHD 路径与 `RECOV_<ID>` 卷标后分离该 VHD，**不要勾选删除虚拟硬盘文件**。脚本不提供按猜测磁盘号强制清理的命令。没有 `result.json` 时也视为未完成，保留工作目录中的日志。

## 已核查的接口与待验证边界

- `diskpart` 的 fixed VHD 创建与 NTFS 格式化是 Windows 自带路径；Storage 的 `Get-DiskImage | Get-Disk` 用于将文件路径关联到实际附加磁盘。[create vdisk](https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-server-2012-r2-and-2012/gg252579%28v%3Dws.11%29)、[Get-DiskImage](https://learn.microsoft.com/en-us/powershell/module/storage/get-diskimage?view=windowsserver2025-ps)
- Shell 删除使用明确绝对路径和双 NUL 终止缓冲区，启用 `FOF_ALLOWUNDO`，检查 API 返回值、操作取消状态和回收站内容。[SHFileOperationW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shfileoperationw)、[SHFILEOPSTRUCTW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ns-shellapi-shfileopstructw)
- `SHEmptyRecycleBinW` 的非空根路径限制到指定卷；空/null 会影响所有卷，因此代码在 PowerShell 验证卷身份后，C# 还要求明确的 D–Z 根路径。查询也始终传同一个根路径。[SHEmptyRecycleBinW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shemptyrecyclebinw)、[SHQueryRecycleBinW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shqueryrecyclebinw)

本机已验证 diskpart/Storage、盘符重新挂载、卷刷新/卸载、Shell 回收与清空、`$I` 关联及合成目标的实际恢复。导出的镜像在扫描/恢复前后摘要一致，测试 VHD 最终全部卸载，已有磁盘身份和布局清单未变。当前配置为 Windows 11 10.0.26200、Windows PowerShell 5.1.26100.9444；其他 Windows 配置和回收站设置仍需覆盖。管理员进程创建的恢复目录已验证可由同一普通用户重新打开。

该材料只代表受控的小型逻辑 NTFS 样本。它没有制造保证的磁盘碎片或特定 MFT 驻留布局，没有测试物理 SSD 删除/TRIM、坏盘、格式化、压缩/加密文件，也没有测量成熟产品的恢复率。不同阶段之间 Windows 自身的元数据操作可能影响此前删除文件的残留，因此每个阶段都保留独立镜像和原件真值。
