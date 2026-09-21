# 自动测试与覆盖矩阵

本文对应 0.3.0rc2。测试按“支持功能、异常输入、资源边界、取消续接、来源与输出保护、界面和交付”组织。用例通过、代码覆盖率、独立原件匹配率是三个不同指标；不能互相替代，也不代表所有可能输入和硬件状态都已穷尽。

## 一条命令运行软件回归

在项目根目录准备开发环境，`artifacts` 父目录需要存在。每次使用新的输出目录：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[desktop,test,audit]'
New-Item -ItemType Directory -Force .\artifacts
.\.venv\Scripts\python.exe -B tools/run_automated_tests.py --output artifacts/tests-new --min-lines 88 --min-branches 78
```

管理员 PowerShell 中可追加 `--require-no-skips`，要求包括 Windows 符号链接保护在内的全部用例实际执行。非管理员环境可能跳过权限受限的用例，Linux 会跳过 Windows 专用测试。跳过原因始终保留，报告状态为 `incomplete`；只有使用 `--require-no-skips` 才会同时因跳过而返回非零。测试失败、没有发现测试或低于指定覆盖门槛均返回非零。

该入口使用临时合成文件和注入故障，不采集真实设备、不创建 VHD、不自动提权、不下载镜像。安装依赖是上面的独立准备步骤。Qt 用例默认离屏运行。

| 文件 | 用途 |
| --- | --- |
| `tests.json` | 每项用例、参数子用例、结果、耗时、跳过原因、环境与覆盖门槛 |
| `tests.log` | 完整 unittest 输出和失败堆栈 |
| `coverage.json` | 每个源文件的执行行、遗漏行、已覆盖和遗漏分支 |
| `coverage.txt` | 文件汇总及未覆盖行号；`Cover` 列是行与分支的合并指标 |
| `html/index.html` | 可点击源文件查看遗漏行和分支的 HTML 报告 |

覆盖统计包含整个 `src/`，没有删除难测模块或排除代码来提高百分比。行覆盖率与分支覆盖率分别计算；未给本进程采集的子进程、TSK 本体和 Qt 原生代码算入覆盖率。真实引擎及窗口验收另行记录。

## 支持功能及错误路径

下表中的名称均位于源码仓库的 `tests/`。正常、空值、非法值和边界组合可在逐项报告中展开。

| 范围 | 自动检查的场景 | 主要用例文件 |
| --- | --- | --- |
| 分区与文件系统 | raw/MBR/GPT/EBR、512/4096 扇区、CRC、分区边界、循环链、畸形启动区、exFAT checksum/FAT/位图/簇堆、拒绝 TexFAT | `test_desktop_core.py`、`test_exfat.py`、`test_input_boundaries.py` |
| NTFS/exFAT 元数据 | 删除文件与目录、空文件、中文和长路径、重复记录、候选/目录预算、未知名称、受控提取 | `test_core.py`、`test_exfat.py`、`test_recycle_bin.py` |
| 回收站与旧日志 | `$I/$R` 关联、索引缺失/损坏、日志页和记录验证、历史记录复用、已分配簇排除、缺口片段、伪造候选拒绝 | `test_recycle_bin.py`、`test_ntfs_log.py` |
| PNG | 签名跨块、CRC、块顺序/尺寸、截断、已分配空间拒绝、读取与候选预算、内容变化、预览导出复核 | `test_carving.py` |
| JPEG | 基线/渐进标记、量化和 Huffman 表、采样/尺寸、扫描头、熵数据、重启标记、逐前缀截断、512 次固定种子变异 | `test_jpeg.py`、`test_jpeg_boundaries.py` |
| JPEG 碎片 | 顺序多段及邻近两段、有效尾部歧义拒绝、越界/重叠描述、分配变化、摘要变化、候选/签名/读取预算 | `test_jpeg.py`、`test_jpeg_boundaries.py` |
| 只读采集 | 文件/卷/整盘身份、扇区和块大小、超时/短读/坏扇区、先正常区再细分重试、零填充、短写循环、满盘、取消、断点和重复续采 | `test_acquisition.py`、`test_acquisition_boundaries.py`、`test_runtime_boundaries.py` |
| 采集状态保护 | 范围覆盖与上限、非法字段、源更换、目标篡改、good 区域摘要、锁、原子进度、失败后保留 pending 状态 | `test_acquisition.py`、`test_acquisition_boundaries.py` |
| 扫描断点 | 元数据目录队列、PNG/JPEG 块游标、完成阶段复用、版本/源/TSK/选项不符、损坏或过大 JSON、非法阶段、并发锁、fsync/替换失败 | `test_scan_resume.py`、`test_resume_boundaries.py`、`test_cli_boundaries.py` |
| 源与目标隔离 | 源摘要变化、真实卷身份契约、同物理盘/无法确定目标拒绝、重开后重新核对、路径穿越、符号/硬链接、Windows ACL | `test_windows_contract.py`、`test_windows_output.py`、`test_core.py`、`test_verification.py`、`test_runtime_boundaries.py` |
| 提取与报告 | 权限、目标空间、部分写入、取消、源断开、未处理数量、报告回退、已有目录/报告不覆盖、片段不算完整恢复 | `test_recovery_failures.py`、`test_cancellation.py`、`test_verification.py` |
| 独立验证 | 删除前原件与阶段摘要、零目标、重复内容一对一匹配、字节/文件名/目录分别计数、阶段/场景/大小分组、失败与跳过不计恢复 | `test_verification.py`、`test_fixture_validation.py`、`test_live_volume_validation.py` |
| 预览 | UTF-8/BOM/UTF-16/GB18030、二进制和短读、图片限制、Office 文字摘录、损坏 ZIP/XML、实体声明拒绝、XML/文档项数/字符数上限 | `test_desktop_core.py`、`test_desktop_ui.py`、`test_input_boundaries.py` |
| 子进程与编码 | stdout/stderr 限额、超时、取消、回收进程、二进制读取协议、pythonw 切换 console、Windows ANSI 严格解码 | `test_tsk_bounds.py`、`test_tsk_encoding.py`、`test_windows_failures.py`、`test_runtime_boundaries.py` |
| CLI | 参数类型和范围、来源互斥、命令分派、失败/部分/取消退出码、验证必须匹配且完成、坏断点返回错误而非堆栈 | `test_extended_cli.py`、`test_cli_boundaries.py` |
| Qt 状态与交互 | 筛选、数字排序、勾选和重置、工作线程信号/节流、取消和关闭、错误提示、预览、恢复报告、扫描/采集续接 | `test_desktop_ui.py`、`test_cancellation.py`、`test_runtime_boundaries.py` |
| 测试与验收入口 | 失败/跳过/子用例的区别、无用例失败、setup 失败、PowerShell 编码/语法、超时清理、恢复率统计 | `test_windows_fixture.py`、`test_windows_validation.py`、`test_runtime_boundaries.py` |

## 真实引擎、窗口与便携包

这些检查使用独立保存的合成原件和冻结镜像，不能被上面的故障注入替代。原件清单只提供给验证器，不给恢复引擎。镜像样本生成方法见 [Windows 验收说明](windows-testing.md)。

```powershell
# 真实 TSK 逐阶段扫描、恢复并与独立原件核对；替换为完整样本目录。
.\.venv\Scripts\python.exe -B -m recovery_core validate-fixture D:\fixtures\exfat-fixture-ID --output artifacts/exfat-new --deep-png --tsk-bin D:\Tools\tsk\bin
.\.venv\Scripts\python.exe -B -m recovery_core validate-fixture D:\fixtures\ntfs-fixture-ID --output artifacts/ntfs-new --deep-png --deep-log --tsk-bin D:\Tools\tsk\bin

# 对直接删除阶段执行原生 Qt 流程，每档缩放单独运行并使用新目录。
.\.venv\Scripts\python.exe -B tools/run_desktop_acceptance.py --image D:\fixtures\after-direct-delete.img --manifest D:\fixtures\after-direct-delete.manifest.json --scale 1.25 --output artifacts/desktop-125-new --tsk-bin D:\Tools\tsk\bin

# 在新镜像副本中放入独立 JPEG 两段，验证重组、取消续扫、预览导出和文件采集。
.\.venv\Scripts\python.exe -B tools/run_extended_acceptance.py --image D:\fixtures\after-direct-delete.img --offset 128 --output artifacts/extended-new --tsk-bin D:\Tools\tsk\bin
```

缩放支持 `1 / 1.25 / 1.5 / 2`，偏移应替换为样本实际值。原生窗口工具检查控件可达、文本/图片预览、结果和原路径、重开会话、取消。其 UAC 取消检查使用故障注入，报告明确区分实际安全桌面操作。`run_extended_acceptance.py` 的碎片是人为散放在空闲区，不能代表 Windows 已验证所有碎片布局。

便携包用自己的 `runtime/python.exe` 执行 `check_package.py` 和 `validation/` 中相同的原生工具；后者必须追加 `--installed-runtime`，并核对报告内实际导入模块位于包目录。开发环境另运行 `tools/audit_windows_pe.py` 检查 PE 依赖。ZIP 的目录项、字节、摘要与已验收解压目录逐项比较，最终报告保存到包外。

GitHub Actions 配置 Linux/Windows × Python 3.11/3.13 的矩阵，并上传每次 JSON、完整日志和 HTML。本地通过不等于远端矩阵已经执行；远端状态以对应工作流为准。

## 仍需外部环境的验收

真实故障盘的超时、掉电/物理拔盘、SSD TRIM 后残留、不同控制器和存储拓扑、真实 UAC 安全桌面取消、跨显示器 DPI 动态切换无法由当前文件样本证明。专用物理介质验收按用户安排暂缓。不支持的文件系统、加密解密和任意格式碎片恢复仍以[桌面说明](desktop-guide.md)的范围为准。

代码遗漏行/分支、以上环境缺口和无法完整恢复的样本分别保留在覆盖报告、本文和阶段验证报告中。测试全绿不表示这些缺口已消失。[本轮实际结果](milestones/12-automated-coverage.md)
