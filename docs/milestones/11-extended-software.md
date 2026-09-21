# 0.3.0rc1 扩展软件开发与验收

日期：2026-09-21。用户要求完成剩余软件开发，专用实物介质验收按用户安排暂缓。本轮四项任务均已提供核心、CLI、桌面入口、回归测试和说明；以列明的实现边界作为完成范围。

- [x] exFAT 删除文件扫描、预览、导出及 PNG/JPEG 内容扫描。
- [x] JPEG 内容查找与有界基线碎片重组，歧义拒绝及不确定性提示。
- [x] 只读镜像采集、正常区域优先、坏区隔离、超时取消和恢复采集。
- [x] 镜像扫描保存进度，核对源文件并继续中断任务。
- [x] 管理员自动回归、原生桌面验收、便携包构建入口和随包文档。

## 实现与约束

exFAT 启动区 checksum、FAT/簇堆边界、分配位图及循环链都有检查。使用真实 TSK 4.15.0 的 exFAT 记录编号提取内容，保留可获得的名称和目录；只支持标准单 FAT exFAT，明确拒绝双 FAT TexFAT。NTFS 旧日志选项不适用于 exFAT。格式依据 [Microsoft exFAT 规范](https://learn.microsoft.com/en-us/windows/win32/fileio/exfat-specification)。

JPEG 连续候选最多 64 MiB、4000 万像素。基线顺序 DCT 检查表、精确块数、熵编码、填充位及重启标记；渐进式只检查标记结构、表和扫描顺序，不用于碎片重组。基线重组最多 8 MiB，尝试最多 32 个顺序空闲段，或前 8 个头部簇边界与 ±4 MiB 内最近的 128 个空闲尾部簇。多个有效不同内容则跳过，搜索边界单独记录。原名与路径未知，重组结果始终提示待核对。依据 [ITU-T T.81](https://www.w3.org/Graphics/JPEG/itu-t81.pdf) 自行实现标准库解析，没有新增图像解析运行依赖。

采集支持文件、Windows 卷和物理磁盘。源只读打开，隐藏子进程限制读取时间，失败区域按大小逐步细分到扇区并有限重试。新目录中的 `image.img` 和 `acquisition.json` 共同保存数据与范围状态；最终坏区用零占位，不能算作恢复内容。先 fsync 数据，再原子替换进度；续采核对源身份、目标身份及已完成范围的摘要。活动源的身份一致不能证明内容不变，软件不提供硬件维修或卷冻结。

镜像扫描保存 `scan-progress.json`，含版本、源 SHA-256、选项、目录队列和 PNG/JPEG 块游标。已完成阶段直接复用，NTFS 日志按阶段重跑；目录锁拒绝并发续接。扫描续接要求软件与 TSK 版本一致、源文件未移动或改变。实时卷应先采集为镜像。导出仍写新目录，避免覆盖已有结果。

## 本机验收结果

环境：Windows 11 x64，Windows PowerShell 5.1，开发 Python 3.13.2，Qt/PySide6 6.8.3，TSK 4.15.0；便携运行时 Python 3.13.12。证据根目录为 `artifacts/extended-software-20260921/`，包含原件、阶段镜像、日志、截图和独立验证报告，不纳入源码仓库或发行包。

| 验收 | 结果 | 证据 |
| --- | --- | --- |
| 管理员全量自动测试 | 277 项通过，0 失败、0 错误、0 跳过 | `admin-final/admin-tests.json`、完整日志 |
| Windows 正常格式化并直接删除 exFAT 文件 | 26 个目标内容、文件名、目录均匹配；包含空文件、边界大小、随机内容、中文和长路径 | `exfat-verification.json`、`exfat-attempt2/exfat-fixture-6fd080fc5ff14f828ee4f39c58c0103c/` |
| 原生桌面 100/125/150/200% 缩放 | 每档 26/26 内容与路径匹配；预览、导出、重开、取消及新控件可达性通过 | `dpi-1/`、`dpi-1.25/`、`dpi-1.5/`、`dpi-2/` |
| 真实 TSK + 人为散放的 JPEG 两段重组 | 独立 28,181 字节 JPEG 完整匹配，段长 4,096 + 24,085；原路径未知，正确路径计数为 0 | `extended-native/extended-acceptance.json`、`verification.json` |
| 扫描取消与续接 | 真实 JPEG 扫描中断后完成，已完成元数据不重复读取；桌面能打开进度并预览导出 | `extended-native/scan/scan-progress.json`、截图 |
| 只读隔离 VHD 的 exFAT 卷恢复 | 内容与路径 26/26，候选与同一 raw 镜像一致；同盘输出与卸载后访问被拒绝 | `live-validation-4b7cc2f9488e43b2980c41430715bafa/material/live-volume.json` |
| 整盘及分区采集 | 整盘 134,217,728 字节与独立 raw 摘要一致；分区 133,103,616 字节在 16 MiB 后取消并续采，最终摘要一致 | 同目录 `device-acquisition.json` |
| 源与主机保护 | 输入 raw、原 VHD、只读副本摘要不变；副本最终卸载，原有磁盘与分区清单不变 | `live-validation-4b7cc2f9488e43b2980c41430715bafa/workflow.json` |

自动测试还覆盖多段重组、不同有效尾部的歧义拒绝、已分配簇拒绝、损坏熵编码、坏扇区定位与仅重读缺失区域、读取超时、权限失败、源更换、目标篡改、并发锁、进度原子替换失败，以及元数据/PNG/JPEG 续接。注入错误不是实际坏盘或物理拔盘实测。失败尝试保留原始日志，最终结果取 `admin-final` 和成功验收目录。

原件与清单只用于输出后的验证，未输入扫描引擎。JPEG 测试在新镜像副本的空闲零区域人为散放，并非 Windows 碎片删除行为。历史 NTFS 追加写入样本仍有不可完整恢复的目标，其 `incomplete` 结论保留，不因本轮软件完成而改变。

## 交付与复验

候选包：`dist/ShiHui-0.3.0rc1-windows-x64.zip`。解压后运行 `Start-ShiHui.cmd`；`Check-Package.cmd` 核对文件清单和实际 Qt/TSK 启动。随包包括应用源码、固定运行时、第三方对应源码与许可、使用说明和验收工具。软件未上传新的 GitHub Release，也未做代码签名。

最终包的清单、147 个 PE 导入检查、ZIP 摘要和包内运行时验收另存于证据根目录的 `package-validation.json`、`package-build.json`、`package-pe.json`、`package-extended/`、`package-ntfs/` 和 `package-exfat-fixture/`。这些输出在包构建之后生成，避免把包摘要写回包内造成循环依赖。

复验命令与数据生成方法见[开发说明](../development.md)和 [Windows 测试说明](../windows-testing.md)。真实故障盘、专用可写物理介质、物理拔盘、真实 UAC 安全桌面取消及跨显示器切换仍待外部环境验收；不承诺找回已覆盖、TRIM 清零或缺失的数据，不宣称优于竞品。
