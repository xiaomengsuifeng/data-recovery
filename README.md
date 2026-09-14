# 拾回 · 免费数据恢复

面向 Windows 普通用户的本地 NTFS 误删文件恢复工具。当前版本 **0.2.0 桌面测试版**，提供来源选择、扫描、筛选、预览、批量导出、任务取消和结果报告；开源、无需账号、无恢复容量收费门槛。

主要解决直接删除、误删文件夹，以及清空回收站后仍留有可读取记录的文件恢复。文件是否能恢复取决于残留数据；不把候选数量或预览成功当作恢复成功率。

## Windows 使用

从 [GitHub Releases](https://github.com/xiaomengsuifeng/data-recovery/releases/tag/v0.2.0) 下载 `ShiHui-0.2.0-windows-x64.zip`，完整解压后双击 `Start-ShiHui.cmd`。便携包自带 Python、Qt 和 TSK，无需安装开发环境。直接扫描磁盘需要管理员权限，软件、工作目录和输出需位于另一块物理磁盘。

这是预发布测试版，尚未完成全部 Windows 真机验收。GitHub 自动生成的 “Source code” 压缩包仅包含项目源码。当前源码另包含 Windows 验收修复、可选 PNG 深度扫描及旧 NTFS 日志恢复，原始 0.2.0 Release 尚未包含这些更新。

- [桌面使用说明、支持范围和常见问题](docs/desktop-guide.md)
- [0.2.0 开发验收与验证边界](docs/milestones/02-desktop-test-release.md)
- [Windows 隔离样本生成与真机验收](docs/windows-testing.md)
- [许可和对应源码说明](THIRD_PARTY_NOTICES.md)

Windows 卷枚举、只读访问、同物理盘拦截和管理员重启已实现。源码已完成原生 Qt 镜像流程、只读卷枚举及管理员隔离 VHD 删除实验。镜像可补充 PNG 内容扫描和旧日志恢复；缺少名称证据时保持原目录未知，有缺口的数据明确标为片段。[最新旧日志恢复验收](docs/milestones/07-ntfs-log-recovery.md)

## 开发与验证

桌面采用 PySide6 / Qt Widgets，核心采用 Python 标准库与独立运行的 The Sleuth Kit `fls` / `icat`。镜像模式自动识别 raw、MBR/GPT 和扩展分区中的 NTFS；真实设备模式支持 Windows 上有盘符的 NTFS 卷。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[desktop,audit]'
PYTHONPATH=src .venv/bin/python -B -m recovery_desktop --tsk-bin /path/to/tsk/bin
PYTHONPATH=src QT_QPA_PLATFORM=offscreen .venv/bin/python -B -m unittest discover -s tests -v
```

开发桌面推荐 Python 3.11–3.13。包内运行时固定为 Python 3.13.12 x64。CLI 的 `doctor / scan / recover / verify / validate-fixture` 可单独使用；CLI 扫描入口限镜像，Windows 磁盘扫描通过桌面提供。

[开发命令、报告格式、可复现演示和 Windows 打包](docs/development.md)。Windows 包由固定上游归档离线组装，`tools/runtime-lock.json` 锁定下载地址、长度、SHA-256 和实际选用模块；包内附有对应开源组件源码与许可。

当前自动测试共 212 项，管理员环境全部通过、无跳过，包含扩展样本验收、旧日志、片段、内容扫描、Qt 和符号链接用例。NIST DFR-01 镜像另用于原生 Qt 和真实 TSK 的桌面回归，其参考摘要来自删除后镜像的文档指定区域。[NIST 样本证据说明](tests/integration/fixture-source.md)

隔离样本已保存 9 个删除前原件并生成四阶段镜像。同时启用 PNG 和旧日志扫描后，阶段内容匹配为 0/0、4/4、2/4、6/8，原路径匹配为 0、4、2、6。另导出 1 份文字片段，不计入完整恢复；仍有 2 个 TXT 未完整恢复，整轮保持 `incomplete`。另一独立生成的同类 Windows 样本得到相同计数。[使用方法](docs/windows-testing.md) · [验收报告](docs/milestones/07-ntfs-log-recovery.md)

另完成两组随机内容和长路径的扩展样本，每组 34 个删除目标。清空回收站后内容及原路径均为 28/34；追加 4 MiB 或 32 MiB 写入后均仅剩 2/34 的 PNG 内容，原路径未知。新增五阶段自动验收及按场景、大小统计，未启用缺乏正文证据的驻留日志恢复。[最新扩展验收](docs/milestones/08-expanded-fixture-validation.md)

## 当前边界

- 支持 NTFS 未命名数据流的元数据恢复；删除目录额外遍历、回收站 `$I/$R` 关联、中文名称与目录导出已实现。
- 镜像可选 PNG 深度扫描，仅支持未分配空间中的连续静态 PNG，最大 256 MiB；校验块结构与 CRC，不恢复原名与路径，可能产生重复或内嵌图片候选。
- 镜像可选旧 NTFS 日志恢复，支持部分日志格式中的非驻留数据及复用记录；片段单独标记、保存和统计，历史名称与内容仍需核对。
- 暂无 FAT/exFAT/APFS、其他格式或碎片文件的内容扫描、BitLocker/EFS 解密、坏盘采集、断点续扫和 TRIM 逆转。
- 镜像模式在关键操作前后比较完整 SHA-256；真实卷只能核对设备身份，Windows 可能继续改变卷内容。
- 清空回收站及删除后追加写入已完成小型隔离 VHD 实测；更多分配布局、物理介质、应用 UAC 取消、原始设备访问及不同 DPI 仍需验证。

## 产品与调研

- [第一版用户、问题与产品范围](docs/product-brief.md)
- [市场功能、实现方式与超越条件](docs/research/market-and-implementation.md)
- [竞品矩阵](docs/research/competitor-matrix.md)
- [开源组件审计](docs/research/opensource-audit.md)
- [公平对照测试方案](docs/research/benchmark-plan.md)
- [历史 0.1 原型验收](docs/milestones/01-ntfs-prototype.md)

研究核查日期为 2026-09-12，桌面版本更新于 2026-09-14。尚未完成竞品实测，不宣称恢复率领先。当前价值是免费导出、清楚的本地流程、可审计的结果与开源实现；算法优势要通过独立样本验证。

## 开源许可与参与

本项目代码采用 [MIT License](LICENSE)。随便携包提供的 Python、Qt/PySide6、TSK 等组件遵循各自许可，见[第三方声明](THIRD_PARTY_NOTICES.md)；对应开源组件源码与许可文本随包提供。

欢迎提交 Issue 或 Pull Request。报告问题时提供软件版本、Windows 版本、介质/文件系统类型、操作步骤与错误信息；请先移除报告中的个人文件名和路径，不要上传个人磁盘镜像或恢复文件。复现与验证方法见[开发说明](docs/development.md)。
