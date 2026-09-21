# 拾回 · 免费数据恢复

面向 Windows 普通用户的本地 NTFS / exFAT 误删文件恢复工具。当前版本 **0.3.0rc2 软件候选版**，提供扫描、筛选、预览、导出、PNG/JPEG 内容扫描、有界 JPEG 碎片重组、只读镜像采集与中断续接；开源、无需账号、无恢复容量收费门槛。

主要解决直接删除、误删文件夹，以及清空回收站后仍留有可读取记录的文件恢复。文件是否能恢复取决于残留数据；不把候选数量或预览成功当作恢复成功率。

## Windows 使用

当前源码构建的候选包为 `ShiHui-0.3.0rc2-windows-x64.zip`，完整解压后双击 `Start-ShiHui.cmd`。便携包自带 Python、Qt 和 TSK，无需安装开发环境。直接读取设备需要管理员权限，软件、工作目录和输出需位于另一块物理磁盘。

本轮扩展自动测试，修复坏区零填充短写和损坏扫描断点的错误处理。专用物理介质验收按用户安排暂缓。0.3.0rc2 为本地交付，尚未上传 GitHub Release；历史 0.2.0 包不含本轮更新。GitHub 自动生成的 “Source code” 压缩包仅包含项目源码。

- [桌面使用说明、支持范围和常见问题](docs/desktop-guide.md)
- [自动测试范围、运行命令与覆盖报告](docs/test-matrix.md)
- [0.3.0rc2 测试结果、修复与交付边界](docs/milestones/12-automated-coverage.md)
- [0.3.0rc1 四项扩展功能与验收证据](docs/milestones/11-extended-software.md)
- [0.2.1rc1 软件收尾、验证方法与交付边界](docs/milestones/10-software-completion.md)
- [0.2.0 开发验收与验证边界](docs/milestones/02-desktop-test-release.md)
- [Windows 隔离样本生成与真机验收](docs/windows-testing.md)
- [许可和对应源码说明](THIRD_PARTY_NOTICES.md)

Windows 卷枚举、只读访问、同物理盘拦截和管理员重启已实现。源码已完成原生 Qt 镜像流程、只读卷枚举及管理员隔离 VHD 删除实验。镜像可补充 PNG 内容扫描和旧日志恢复；缺少名称证据时保持原目录未知，有缺口的数据明确标为片段。[最新旧日志恢复验收](docs/milestones/07-ntfs-log-recovery.md)

## 开发与验证

桌面采用 PySide6 / Qt Widgets，核心采用 Python 标准库与独立运行的 The Sleuth Kit `fls` / `icat`。镜像模式自动识别 raw、MBR/GPT 和扩展分区中的 NTFS / 标准单 FAT exFAT；直接扫描支持 Windows 上有盘符的这两类卷。整盘可只读采集为 raw 镜像。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[desktop,test,audit]'
PYTHONPATH=src .venv/bin/python -B -m recovery_desktop --tsk-bin /path/to/tsk/bin
.venv/bin/python -B tools/run_automated_tests.py --output test-results
```

开发桌面推荐 Python 3.11–3.13。包内运行时固定为 Python 3.13.12 x64。CLI 提供 `doctor / scan / resume-scan / acquire / resume-acquire / recover / verify / validate-fixture`；CLI 扫描入口限镜像，Windows 卷扫描通过桌面提供。

[开发命令、报告格式、可复现演示和 Windows 打包](docs/development.md)。Windows 包由固定上游归档离线组装，`tools/runtime-lock.json` 锁定下载地址、长度、SHA-256 和实际选用模块；包内附有对应开源组件源码与许可。

管理员自动测试 359 项全部通过，含 1,710 个参数子用例，无失败或跳过；本机 Python 测试进程的源码行覆盖率 90.63%、分支覆盖率 81.93%，未排除源文件。[最新验收记录](docs/milestones/12-automated-coverage.md)列出各项证据及未覆盖边界。真实 Windows exFAT 删除实验的内容、文件名和目录匹配均为 26/26；JPEG 碎片另用独立原件核验。原生窗口验收覆盖 100%、125%、150%、200% 缩放。NIST 历史样本的参考摘要来自删除后指定磁盘区域，[证据边界单独记录](tests/integration/fixture-source.md)。

隔离样本已保存 9 个删除前原件并生成四阶段镜像。同时启用 PNG 和旧日志扫描后，阶段内容匹配为 0/0、4/4、2/4、6/8，原路径匹配为 0、4、2、6。另导出 1 份文字片段，不计入完整恢复；仍有 2 个 TXT 未完整恢复，整轮保持 `incomplete`。另一独立生成的同类 Windows 样本得到相同计数。[使用方法](docs/windows-testing.md) · [验收报告](docs/milestones/07-ntfs-log-recovery.md)

另完成两组随机内容和长路径的扩展样本，每组 34 个删除目标。清空回收站后内容及原路径均为 28/34；追加 4 MiB 或 32 MiB 写入后均仅剩 2/34 的 PNG 内容，原路径未知。新增五阶段自动验收及按场景、大小统计，未启用缺乏正文证据的驻留日志恢复。[最新扩展验收](docs/milestones/08-expanded-fixture-validation.md)

已新增只读虚拟卷的直接扫描验收入口，实际验证桌面选择卷、预览、保存和重开，同盘输出及卷卸载后的访问会被拒绝。直接删除样本内容与路径均为 4/4，与同一 raw 镜像一致；输入和副本摘要未变。[最新直接卷验收](docs/milestones/09-live-volume-validation.md)

## 当前边界

- 支持 NTFS 未命名数据流和标准 exFAT 的删除记录恢复；删除目录额外遍历、回收站 `$I/$R` 关联、中文名称与目录导出已实现。
- 镜像可选 PNG 深度扫描，仅支持未分配空间中的连续静态 PNG，最大 256 MiB；校验块结构与 CRC，不恢复原名与路径，可能产生重复或内嵌图片候选。
- 镜像可选旧 NTFS 日志恢复，支持部分日志格式中的非驻留数据及复用记录；片段单独标记、保存和统计，历史名称与内容仍需核对。
- JPEG 内容扫描最多 64 MiB / 4000 万像素；基线 JPEG 碎片重组最多 8 MiB，可尝试顺序空闲段或邻近两段拼接。歧义结果跳过，重组结果明确标为待核对，不推断原名与路径。
- 只读采集支持文件、Windows 卷和整盘，先读取正常区域，再缩小失败区域并有限重试；记录坏区、取消和续采进度。镜像扫描保存元数据目录队列及 PNG/JPEG 块进度，可继续中断任务。
- 暂不支持 FAT12/16/32、TexFAT、APFS、任意格式的碎片重组、BitLocker/EFS 解密、硬件维修和 TRIM 逆转。
- 镜像模式在关键操作前后比较完整 SHA-256；真实卷只能核对设备身份，Windows 可能继续改变卷内容。
- 清空回收站、追加写入和只读虚拟卷的直接访问已完成隔离 VHD 实测；软件故障分支有自动回归。可写物理介质、物理拔盘、真实 UAC 取消及跨显示器 DPI 切换仍待外部环境验收。

## 产品与调研

- [第一版用户、问题与产品范围](docs/product-brief.md)
- [市场功能、实现方式与超越条件](docs/research/market-and-implementation.md)
- [竞品矩阵](docs/research/competitor-matrix.md)
- [开源组件审计](docs/research/opensource-audit.md)
- [公平对照测试方案](docs/research/benchmark-plan.md)
- [历史 0.1 原型验收](docs/milestones/01-ntfs-prototype.md)

研究核查日期为 2026-09-12，桌面版本更新于 2026-09-21。尚未完成竞品实测，不宣称恢复率领先。当前价值是免费导出、清楚的本地流程、可审计的结果与开源实现；算法优势要通过独立样本验证。

## 开源许可与参与

本项目代码采用 [MIT License](LICENSE)。随便携包提供的 Python、Qt/PySide6、TSK 等组件遵循各自许可，见[第三方声明](THIRD_PARTY_NOTICES.md)；对应开源组件源码与许可文本随包提供。

欢迎提交 Issue 或 Pull Request。报告问题时提供软件版本、Windows 版本、介质/文件系统类型、操作步骤与错误信息；请先移除报告中的个人文件名和路径，不要上传个人磁盘镜像或恢复文件。复现与验证方法见[开发说明](docs/development.md)。
