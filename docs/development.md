# 拾回 0.3.0rc2 开发说明

核心支持 NTFS / 标准 exFAT 镜像及 Windows 卷，桌面采用 PySide6 / Qt Widgets，元数据读取由独立 TSK 4.15.0 CLI 提供。JPEG 解析、只读采集和进度记录使用 Python 标准库，无新增核心依赖；桌面增加 PySide6-Essentials。自己的代码采用 MIT，第三方组件单独按其许可使用，见[第三方声明](../THIRD_PARTY_NOTICES.md)。

## 本机桌面开发

使用 Python 3.11–3.13，并准备同版本的 `fls` 和 `icat`：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[desktop,test,audit]'
PYTHONPATH=src .venv/bin/python -B -m recovery_desktop --tsk-bin /absolute/path/tsk/bin
```

Windows 开发环境使用 `.venv\Scripts\python.exe`。便携包用户直接运行 `Start-ShiHui.cmd`，无需以上步骤。`RECOVERY_TSK_BIN` 可指定工具目录；打包启动器自动设置它。

PowerShell 开发环境可使用以下命令，TSK 目录替换为本机实际位置：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[desktop,test,audit]'
.\.venv\Scripts\python.exe -B -m recovery_desktop --tsk-bin C:\Tools\sleuthkit-4.15.0-win32\bin
```

`src/recovery_core` 包含分区探测、扫描/导出、回收站关联、只读卷访问、预览、原件校验及隔离样本的分阶段验收。`src/recovery_desktop` 包含实际窗口、可筛选的候选模型和 QThread 任务。工作线程报告进度，合作式取消传播到哈希循环和子进程执行器。

## CLI

CLI 的扫描命令限定 raw 镜像，真实卷扫描通过桌面入口提供。

```sh
export PYTHONPATH="$PWD/src"
python3 -B -m recovery_core doctor --tsk-bin /path/to/tsk/bin
python3 -B -m recovery_core scan /path/to/disk.img --offset 2048 --output /path/to/scan-01 --tsk-bin /path/to/tsk/bin
python3 -B -m recovery_core recover /path/to/scan-01 --destination /path/to/recovered-01 --tsk-bin /path/to/tsk/bin
python3 -B -m recovery_core verify /path/to/recovered-01 --manifest /path/to/targets.manifest.json --output /path/to/verification-01.json
python3 -B -m recovery_core validate-fixture /path/to/ntfs-fixture-ID --output /path/to/validated-01 --tsk-bin /path/to/tsk/bin
```

偏移必须替换为实际文件系统起始扇区，分区镜像通常为 0。扇区大小默认 512，可使用 `--sector-size`。桌面自动读取布局。新建扫描、采集、导出需要新目录且父目录已存在；续接只接受本程序生成的进度目录。

`recover --id <候选ID>` 可以重复，限定目标。CLI 默认单文件上限 1 GiB，可用 `--max-file-bytes` 改变；桌面不采用该默认上限，没有收费额度。实际导出受空间、源文件状态及目标文件系统约束。

`scan --deep-png` 额外扫描所选 NTFS / exFAT 分区的未分配空间，`validate-fixture --deep-png` 在各阶段使用同一选项。默认仍只扫描删除记录。核心 `carving.py` 根据 NTFS `$Bitmap` 或 exFAT 分配位图找到连续未分配范围，只以 PNG 签名、块长度、关键块顺序、IHDR 字段、CRC 和 IEND 判断边界；不读取原件清单或推断原名。内容深度扫描不接受实时卷。

`validate-fixture` 接收 `New-RecoveryFixture.ps1` 生成的完整目录，先核对删除前原件和阶段镜像/清单，再逐阶段扫描、导出、验证。NTFS 为四阶段，可附加第五个写入阶段；exFAT 为删除前、直接删除后两阶段。输出在样本目录之外，几何参数自动核对；内容及路径全部匹配才退出 0。[Windows 自动生成与验收方法](windows-testing.md)

## 扩展功能 CLI 与进度格式

```powershell
.\.venv\Scripts\python.exe -B -m recovery_core scan D:\fixtures\disk.img --offset 128 --deep-jpeg --reassemble-jpeg --output D:\results\scan-new --tsk-bin D:\Tools\tsk\bin
.\.venv\Scripts\python.exe -B -m recovery_core resume-scan D:\results\scan-new --tsk-bin D:\Tools\tsk\bin
.\.venv\Scripts\python.exe -B -m recovery_core acquire --file D:\fixtures\disk.img --output D:\results\image-new --retries 1 --timeout 30
.\.venv\Scripts\python.exe -B -m recovery_core resume-acquire D:\results\image-new
```

设备采集可将 `--file` 换成 `--volume E:` 或 `--disk <核对过的磁盘编号>`，需要管理员权限以及另一物理盘上的程序和目标。单次读取由隐藏子进程以 `rb` 执行；超时、取消会结束并回收子进程。先完成大块正常读取，再按 64 KiB / 4 KiB / 逻辑扇区缩小失败范围，额外重试 0–10 次。`--block-bytes` 默认为 16 MiB，上限 64 MiB，必须扇区对齐。

`acquisition.json` 的 `ranges` 连续覆盖源大小，状态为 `good / bad / pending`；good 区域保存 SHA-256，最终 bad 区域用零占位。文件写入并 fsync 后，才原子发布进度；重启会核对源身份、目标 inode/尺寸及 good 区域摘要。文件源核对路径、inode、设备、大小及 mtime；设备源核对磁盘身份和布局，不证明活动源内容未变。目录锁防止并发续采。范围记录最多 200,000 项，JSON 最多 64 MiB。采集退出码：完整 0、含坏区 1、失败 2、取消 130。

`scan-progress.json` 记录软件/TSK 版本、完整镜像摘要、选项及各阶段状态。元数据保存已访问目录与剩余队列；PNG/JPEG 每个扫描块保存游标、候选及资源预算；已完成阶段直接复用。重启需要版本、源路径和 SHA-256 一致。NTFS 日志按阶段重新执行，实时卷不续扫。写入临时文件后原子替换，失败保留之前进度；最终 `session.json` 不覆盖。

`jpeg.py` 校验 SOI/EOI、量化表、Huffman 表、扫描头、尺寸和采样；基线顺序 DCT 消耗精确数量的块与熵数据，并检查填充和重启标记。渐进式仅检查标记、表与扫描顺序，标为 `progressive_markers`。连续 JPEG 上限 64 MiB / 4000 万像素；基线重组上限 8 MiB，最多 32 个物理顺序空闲段，或前 8 个头部簇边界与 ±4 MiB 内最近的 128 个空闲尾部簇。多个有效不同内容则跳过，达到搜索边界会记录。所有重组候选仍为待核对，最多 100,000 次签名尝试，额外读取预算为 max(16 MiB, 分区完整簇空间的两倍)。

JPEG 候选使用 `jpeg_carving`，保留字节区间、内容摘要、检查层级、重组标志，原名/目录为空。预览与导出重新核对全部区间未分配、结构及摘要；不把结构有效当成独立原件验证。exFAT 使用数字记录编号及 `exfat_metadata`，几何校验包括启动区 checksum、FAT/堆边界和分配位图；双 FAT TexFAT 明确拒绝。

## 报告与路径

- `session.json`：来源、几何参数、TSK 版本、候选记录、名称证据、警告与局限。
- `recovery.json`：每个输出路径、字节数、SHA-256、导出/部分/失败/跳过状态，以及来源复核状态。`destination` 记录结果根目录，`saved_path` 相对此目录。目标无法写入报告时，改存扫描目录下新的 `recovery-<随机ID>.json`，返回 `report_path`、`report_warning`；两处都失败则明确报错。
- `verification.json`：重新读取导出字节，按独立原件清单一对一匹配，分别统计内容、名称、目录。
- `fixture-validation.json`：隔离样本各阶段的状态与匹配结果；明确区分不同删除目标数和跨阶段验证次数。

输出为 `files/<安全处理后的原目录>/<文件名>`；保留中文，重名用候选 ID 和必要的递增后缀避让。危险、保留或过长的路径片段被编码或缩短，原始记录保存在报告。不把镜像中的绝对路径当作宿主写入路径。没有 `$I` 证据的 `$R` 内容仍可导出，但原名记为未知。

内容候选使用 `recovery_method: png_carving`、空 `inode`、空 `original_path` 和 `path_evidence: content_only`；`carving` 描述镜像绝对字节偏移、扫描时摘要和检查范围。其他候选省略方法时兼容旧的 `ntfs_metadata` 会话。预览和导出共用提取入口，重新核对分区边界、分配位图、PNG 结构、扫描摘要及实际复制摘要；拒绝伪造的原路径、未知方法和越界描述。PNG 保存在 `files/Carved/PNG/`，重复内容不自动合并为同一原文件。

`scan --deep-log` 与 `validate-fixture --deep-log` 启用旧日志恢复，可与 `--deep-png` 同用。候选方法为 `ntfs_log`，描述中保留记录号/序列号、日志证据位置、历史原大小、逻辑偏移、物理区段及摘要。`content_status: fragment` 始终按 `partial` 导出，文件名包含 `.fragment-<偏移>`；验证器读回其字节但排除完整匹配资格。重新打开时从镜像重新推导候选，不能用修改会话来指定任意范围或名称。[支持范围和证据](milestones/07-ntfs-log-recovery.md)

Windows 私有输出目录显式授予当前用户 SID、SYSTEM 和 Administrators 访问权，子目录继承该 ACL；同一用户退出提权后仍可打开文件。Windows TSK 的文本管道按原生 ANSI 代码页严格解码，Unix 保持 UTF-8。本机实测 CP936 的直接删除中文名称；其他语言环境及代码页无法表示的名称仍需验证，不按逐条“猜编码”替换名称。

原件清单格式：

```json
{
  "schema_version": 1,
  "files": [
    {"original_path": "Z:\\Documents\\example.txt", "size": 5, "sha256": "<原件的64位十六进制SHA-256>"}
  ]
}
```

原件清单只给验证器，不提供给扫描引擎。零目标不会显示 100%，一个输出不重复匹配多个目标。预览、解码、长度和哈希存在都不能代替与原件的实际比较。

CLI 退出码：0 为完成（`verify` 还要求全部目标内容匹配且恢复任务完成），1 为文件级未全完成/验证未全匹配，2 为配置或运行失败，130 为取消。

## 来源保护与资源边界

镜像扫描/预览/导出前后比较 SHA-256，适合保持不变的 raw 文件，会增加大镜像耗时。Windows 模式通过只读 PowerShell 查询卷和物理磁盘，读取 `\\.\X:`，核对身份、容量、磁盘 ID。程序、扫描记录、工作目录及目标必须在另一块可确认的本地物理磁盘；重新打开会话后也重新核对。

程序不格式化、修复、锁定、冻结、创建快照或写入源卷。TSK 列表/版本输出与预览保存在内存，回收站索引临时读取使用指定工作目录。启动器禁止字节码写入。Windows 自身仍可能写入活动卷，因此 `live_volume_identity_only` 不等于镜像的 `unchanged` 内容证明。基本 NTFS 卷之外的存储拓扑和真实 Windows 行为仍需验收。

TSK stdout 按预算读取、stderr 最多 1 MiB，同时排空两条管道；超限、超时、取消会终止并回收子进程。单次列表最多 32 MiB，候选最多 100,000，删除目录额外访问最多 2,000。桌面每次 TSK 调用超时 600 秒，CLI 默认 120 秒可调整。

PNG 以 1 MiB 块搜索、64 KiB 块校验，支持跨读取块边界的签名，但不跨已分配簇拼接。位图上限 64 MiB、单 PNG 上限 256 MiB、块数及签名尝试分别最多 100,000；重复签名解析的总读取预算为分区完整簇字节数的两倍。达到扫描上限报错，不写已完成会话。结构解析不展开图像压缩数据；实际解码由有内存和像素上限的 Qt 预览单独执行。

预览最多读取 32 MiB；Office ZIP 的 XML 每项不超过 2 MiB，最多 12 项，拒绝实体声明。图片限制解码内存与像素数。导出前检查可用空间，实际写入失败按逐文件异常保留结果。取消导出时保留报告，重新导出使用新目录；镜像扫描及采集可使用上述进度续接。

## 可复现验证

```sh
.venv/bin/python -B tools/run_automated_tests.py --output test-results
.venv/bin/python -B tools/run_nist_demo.py --fixture-cache artifacts/fixtures --output artifacts/nist-new --tsk-bin /path/to/tsk/bin
QT_QPA_PLATFORM=offscreen .venv/bin/python -B tools/run_desktop_demo.py --output artifacts/desktop-new --tsk-bin /path/to/tsk/bin
```

覆盖入口需要 `[desktop,test]` 开发依赖，输出每项测试/子用例、日志、JSON 和可浏览的 HTML；新目录不会覆盖之前证据。当前完整套件为 359 项，包含四阶段验收、Windows 兼容、PNG/JPEG、旧日志、片段、采集/扫描续接、异常处理及界面回归。管理员全量记录及覆盖率见[最新验收报告](milestones/12-automated-coverage.md)，逐功能检查及参数见[测试矩阵](test-matrix.md)。自动套件与实际 VHD 删除实验分别保存证据。`run_desktop_demo.py` 操作实际 Qt 窗口和工作线程，输出截图、扫描/恢复/校验报告；离屏渲染适用于开发 CI，不等于 Windows 原生窗口或真实磁盘测试。

`tools/run_desktop_acceptance.py` 对独立原件清单和 NTFS 镜像执行完整原生窗口验收，通过 `--scale 1 / 1.25 / 1.5 / 2` 分别启动不同缩放的 Qt 进程；不改系统设置。它检查窗口范围、按钮可达、文本/图片预览、恢复内容与原路径、会话重开和取消后的状态。便携包带有同一工具，使用 `--installed-runtime` 保证测试包内模块。[执行方法](milestones/10-software-completion.md)

`tools/run_png_carving_demo.py --image /path/to/image.img --offset 128 --output /path/to/new-output --tsk-bin /path/to/tsk/bin` 使用实际 Qt/TSK 扫描、解码 PNG、导出并重新打开会话。偏移按实际镜像填写，Windows 设置 `QT_QPA_PLATFORM=windows` 可验证原生窗口；工具仅核对导出与扫描时字节一致，删除前原件仍由 `validate-fixture` 独立验证。

`tools/run_ntfs_log_demo.py` 接受同样参数，验证历史文件及片段的扫描、预览、导出和会话重开；其输入需包含至少一个历史候选及片段。报告明确记录片段状态和实际导入模块位置。两个演示工具都支持 `--installed-runtime` 测试实际便携包。

使用便携包的 `runtime/python.exe` 执行该工具时加 `--installed-runtime`，避免工具默认将工作区源码放到导入路径前面。演示报告包含实际核心与桌面模块路径，便于核对测试对象确实是包内代码。

Windows 可用统一入口保存测试日志、环境信息及实际 Qt/TSK 演示结果。先准备 `artifacts` 父目录，将工具路径替换为本机实际位置；每次 `--output` 使用新目录：

```powershell
New-Item -ItemType Directory -Force .\artifacts
.\.venv\Scripts\python.exe -B tools\validate_windows.py --output '.\artifacts\Windows 验证 01' --tsk-bin C:\Tools\sleuthkit-4.15.0-win32\bin --qt-platform windows
```

该入口运行全部单元测试、TSK 启动检查、Windows PowerShell 5.1 语法检查、只读卷枚举，以及 NIST 镜像桌面演示；不创建测试卷或触发 UAC。`--qt-platform windows` 会短暂显示原生窗口，默认 `offscreen`。没有缓存时会下载并校验固定 NIST 样本，解压需要约 1 GiB 空间。输出中的 `tests.json` 逐项记录跳过原因，`validation.json` 汇总步骤结果，`desktop/` 保存截图和恢复证据。失败或超时返回非零退出码，已有输出不会覆盖。

普通用户运行时，符号链接用例可能因权限不足而跳过；最新管理员套件为 359 项，全部通过且无跳过。覆盖入口 `run_automated_tests.py --require-no-skips` 可要求无跳过验收。统一入口中的 `windows_image_workflow_verified` 仅表示镜像流程，`full_windows_acceptance_verified` 保持为 `false`。实际 VHD 实验的组合场景仍为 `incomplete`，不能用自动测试通过覆盖该结果。[最新记录与边界](milestones/12-automated-coverage.md)

演示使用 NIST DFR-01 镜像，导出 4,296 字节的 `Bunda.txt`，与文档指定的删除后磁盘区域核对。这不是独立删除前原件，也不是普遍恢复率证据。[样本说明](../tests/integration/fixture-source.md)

[Windows 隔离样本脚本](windows-testing.md)已在管理员会话实际生成样本、执行 Shell 清空并自动验收。四阶段目标数为 0、4、4、8；单独元数据恢复匹配数为 0、4、0、4。增加 PNG 扫描后内容为 0、4、1、5；再启用旧日志后内容、原路径均为 0、4、2、6，另有未计入完整匹配的片段，整轮 `incomplete`。[镜像恢复结果](milestones/07-ntfs-log-recovery.md)

`tools/windows/Invoke-LiveVolumeValidation.ps1` 对现有合成样本创建只读 VHD 副本，通过实际“选择磁盘”入口扫描、预览、导出和重开，再与同阶段 raw 镜像和独立原件比较。它还检查同盘输出及卸载后的访问拒绝，最终核对源文件摘要和已有磁盘/分区清单。开发环境直接删除目标匹配 4/4；最新便携包清空回收站后的普通扫描匹配 4/8，均与相应镜像基线一致。此处 `passed` 指流程和保护检查通过，恢复匹配数单列；真实物理介质仍待覆盖。[命令与证据](milestones/09-live-volume-validation.md)

## 构建 Windows 便携包

可在 macOS、Linux 或 Windows 组装，无需交叉编译；必须使用锁定的 Windows 归档，不能把主机的虚拟环境复制进去。

```sh
python3 tools/fetch_windows_runtime.py
python3 tools/build_windows_bundle.py
.venv/bin/python tools/audit_windows_pe.py dist/ShiHui-0.3.0rc2-windows-x64 --output artifacts/windows-pe-audit.json
python3 dist/ShiHui-0.3.0rc2-windows-x64/check_package.py
```

`tools/runtime-lock.json` 记录版本、来源、长度、SHA-256、选用模块和对应源码。下载器只下载构建依赖，软件恢复过程中不联网。错误归档或 `.incomplete` 文件会保留并报错，检查后移走该文件再重试。

离线构建器核对全部归档，创建新的 `dist` 目录与 ZIP，加入启动器、应用源码、使用说明、隔离样本生成及串联验收脚本、第三方对应源码/许可和逐文件清单。不含测试镜像、恢复数据、开发路径或虚拟环境。输出已存在时用 `--output` 指定新名称。已发布的原始 0.2.0 包没有本轮新增入口，需要从当前源码重新构建。

`audit_windows_pe.py` 需 `[audit]` 开发依赖，静态核对 PE 位数及导入库。Python/Qt 是 x64，TSK 是单独的 x86 进程，运行库分开放置；此检查不能证明 Windows 动态加载行为。`check_package.py` 在非 Windows 仅核对清单，在 Windows 还实际启动 Qt 和 TSK。

尚未签名，也未完成全部 Windows 实机验收。原始发布包的启动检查记录保留在[历史 Windows 镜像记录](milestones/03-windows-image-validation.md)。已实际运行管理员隔离 VHD 删除实验和只读虚拟卷的设备读取，并修复脚本兼容、中文名称和提权输出目录问题；断连、权限、满盘和 UAC 取消的软件处理已自动验证，物理拔盘和真实安全桌面取消仍待外部环境验收。

包含 PNG 深度扫描的本地包以 `ShiHui-0.2.0-png-recovery-windows-x64` 命名，原始 0.2.0 Release、fixture-validation 和 windows-acceptance 本地包不含新增内容扫描。构建与验收证据见[内容扫描验收记录](milestones/06-png-content-recovery.md)。本轮没有发布新的 GitHub Release。

旧日志功能包 `ShiHui-0.2.0-log-recovery-windows-x64` 新增历史文件与片段恢复。[旧日志验收摘要](milestones/07-ntfs-log-recovery.md)

扩展样本验收包 `ShiHui-0.2.0-validation-windows-x64` 新增 `-Profile expanded -WritePressureMiB 4` 样本生成配置，以及 `Invoke-RecoveryValidation.ps1 -DeepPng -DeepLog` 串联选项。验证器支持追加写入后的第五阶段，并按删除场景和精确字节大小统计目标；该包对两组冻结基本样本和两组独立扩展样本完成实际复验。[结果与证据边界](milestones/08-expanded-fixture-validation.md)

上一轮本地包 `ShiHui-0.2.0-volume-validation-windows-x64` 包含直接卷验收脚本、Python 辅助工具和重新挂载盘符修复。包内 398 个文件清单及 147 个 PE 检查通过，实际包内 Qt/TSK 完成清空回收站阶段的只读虚拟卷验收。该轮 221 项管理员测试无失败、无跳过。[历史验收及包摘要](milestones/09-live-volume-validation.md)

当前候选包为 `ShiHui-0.3.0rc2-windows-x64`，包含 exFAT、JPEG 内容扫描与有界重组、只读采集及扫描续接，并修复坏区零填充短写和损坏断点的处理。359 项管理员测试全部通过，无失败或跳过。包内运行时与发布 ZIP 的最终校验另存到验收材料，不修改已生成的包。rc1 未完成扫描的断点仍需原版本继续，或在 rc2 重新扫描；已完成扫描记录不受此版本限制。尚未发布新的 GitHub Release。[本轮交付状态](milestones/12-automated-coverage.md)
