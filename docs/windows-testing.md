# 在 Windows 11 生成 NTFS 恢复测试材料

状态：脚本已编写并做静态检查，**尚未在 Windows 11 执行**。它生成恢复测试材料，不是恢复软件，也不证明恢复率。第一轮样本只有合成 TXT 和 PNG；Office 文档、实际拍摄照片、真实移动介质和系统盘行为仍需要后续样本验证。

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

## 运行方法

使用 0.2.0 便携包时，完整解压后在管理员 PowerShell 中进入软件根目录，运行 `powershell.exe -NoProfile -STA -File .\validation\New-RecoveryFixture.ps1 -OutputParent C:\RecoveryFixtures`。先创建普通目录 `C:\RecoveryFixtures`，后续步骤与源码版本相同。生成镜像后可直接在拾回界面选择对应 `.img`，工作目录另选普通文件夹。

需要 Windows 11 的 **64 位 Windows PowerShell 5.1**、管理员身份、内置 Storage 模块和约 **1 GiB 可用空间**。不需要 Hyper-V 可选组件、WSL、Python、Office 或额外下载。

将项目复制到 Windows，例如 `C:\data-recovery`。在“以管理员身份运行”的 Windows PowerShell 中，准备一个本地普通目录，然后运行：

```powershell
New-Item -ItemType Directory -Path C:\RecoveryFixtures
powershell.exe -NoProfile -STA -File C:\data-recovery\tools\windows\New-RecoveryFixture.ps1 -OutputParent C:\RecoveryFixtures
```

若父目录已经存在，跳过 `New-Item`。每次脚本运行都会另建随机子目录，不会覆盖上一轮。父目录必须是本地目录，且目录及祖先不能为重解析点；UNC 网络路径、目录联接、符号链接会被拒绝。脚本不需要访问另一台电脑，也不会请求其远程凭据。

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

## 为什么导出 `.img`，而不直接改名 `.vhd`

固定 VHD 的数据与动态/差分 VHD 的映射方式不同。本脚本只接受自己刚创建的 fixed VHD，检查末尾 512 字节 footer 的 `conectix` 标记、格式版本、磁盘类型 2、容量、固定盘数据偏移标记和 checksum。只有检查通过且文件长等于虚拟容量加 512 字节时，才复制前 128 MiB 为 raw `.img`；原 VHD 不被截断。动态 VHD/VHDX 不适用这个操作。[Microsoft VHD footer 实现](https://github.com/microsoft/azure-vhd-utils/blob/master/vhdcore/footer/footer.go)

`.img` 是**整盘镜像**，前面仍含分区表；NTFS 通常从非零偏移开始。TSK 原始镜像读取应使用 `stages.json` 对应的 `offset_sectors`，单位固定为 512 字节，不要把字节数当扇区数。不能以某次常见的 2048 替代实际记录。

## 将材料用于 Mac CLI

也可在 Windows 便携包内核对桌面导出的结果。进入软件根目录，将以下路径替换成实际值：

```powershell
.\runtime\python.exe -B -m recovery_core verify "D:\Results\recovered-实际编号" --manifest "C:\RecoveryFixtures\ntfs-fixture-实际编号\after-empty-recycle-bin.manifest.json" --output "D:\Results\verification.json"
```

`--output` 要求新文件。验证器重新读取导出字节，与删除前保存的原件摘要对照，不依赖扫描报告声称的哈希。

把完整的 `ntfs-fixture-<ID>` 子目录复制回 Mac。先核对 `.img` 的 SHA-256 与 `stages.json`；恢复引擎只输入故障后的 `.img`，原件和清单由验证阶段使用。

在项目根目录、已经配置好 CLI 依赖的环境中，按下列流程执行。将示例路径和偏移替换成该轮材料的实际值；session 与 destination 都选择尚不存在的新目录：

```sh
python -m recovery_core scan /path/to/ntfs-fixture-ID/after-direct-delete.img --output /path/to/sessions/direct-01 --offset 2048
python -m recovery_core recover /path/to/sessions/direct-01 --destination /path/to/recovered/direct-01
python -m recovery_core verify /path/to/recovered/direct-01 --manifest /path/to/ntfs-fixture-ID/after-direct-delete.manifest.json
```

然后对 `after-empty-recycle-bin.img` 使用新的 session 和 destination，以及它自己的 manifest。内容原样一致、原文件名正确、原目录正确、错误 `$I/$R` 关联分别评估；仅输出 `$R...` 名称不能算原名恢复。流程成功退出也不能替代逐目标内容校验。

## 失败时如何处理

脚本出错会保留已有材料和 `status: failed`，并尝试只卸载本轮 VHD。不要执行“清空回收站”桌面菜单来补做步骤，也不要修改脚本把根路径改成空字符串。

若日志表示未找到正确的 `$I/$R`、数量不符或 Shell 回收失败，该轮不构成清空回收站测试成功。Windows 回收站配置可能让 Shell 直接删除合成文件；脚本会通过缺失的对应证据检测并停止，不会把它写成真实回收站阶段。修正测试环境后新开一轮，保留旧日志；不应为了成功而禁用上述检查。

若最终提示无法卸载 VHD，关闭仅属于临时测试盘的窗口，然后在“磁盘管理”里核对 VHD 路径与 `RECOV_<ID>` 卷标后分离该 VHD，**不要勾选删除虚拟硬盘文件**。脚本不提供按猜测磁盘号强制清理的命令。没有 `result.json` 时也视为未完成，保留工作目录中的日志。

## 已核查的接口与待验证边界

- `diskpart` 的 fixed VHD 创建与 NTFS 格式化是 Windows 自带路径；Storage 的 `Get-DiskImage | Get-Disk` 用于将文件路径关联到实际附加磁盘。[create vdisk](https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-server-2012-r2-and-2012/gg252579%28v%3Dws.11%29)、[Get-DiskImage](https://learn.microsoft.com/en-us/powershell/module/storage/get-diskimage?view=windowsserver2025-ps)
- Shell 删除使用明确绝对路径和双 NUL 终止缓冲区，启用 `FOF_ALLOWUNDO`，检查 API 返回值、操作取消状态和回收站内容。[SHFileOperationW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shfileoperationw)、[SHFILEOPSTRUCTW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ns-shellapi-shfileopstructw)
- `SHEmptyRecycleBinW` 的非空根路径限制到指定卷；空/null 会影响所有卷，因此代码在 PowerShell 验证卷身份后，C# 还要求明确的 D–Z 根路径。查询也始终传同一个根路径。[SHEmptyRecycleBinW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shemptyrecyclebinw)、[SHQueryRecycleBinW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shqueryrecyclebinw)

待 Windows 真机验证的事项包括 diskpart/Storage 兼容性、重新挂载时盘符是否保持、卷刷新/卸载、Shell 回收设置、`$I` 格式、日志与 CLI 的实际恢复输出。当前开发 Mac 没有 PowerShell 运行时；静态阅读不等于 PowerShell 解析器或 Windows API 测试通过。

该材料只代表受控的小型逻辑 NTFS 样本。它没有制造保证的磁盘碎片或特定 MFT 驻留布局，没有测试物理 SSD 删除/TRIM、坏盘、格式化、压缩/加密文件，也没有测量成熟产品的恢复率。不同阶段之间 Windows 自身的元数据操作可能影响此前删除文件的残留，因此每个阶段都保留独立镜像和原件真值。
