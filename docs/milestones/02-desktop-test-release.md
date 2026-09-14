# 0.2.0 NTFS 桌面测试版

完成日期：2026-09-14。本阶段实现完整桌面恢复流程并交付 Windows 便携包，之后进行 Windows 11 真机验收。此版本是限定 NTFS 范围的桌面测试版，尚不是完成实机认证的正式发布版。

本文保留发布时的验证记录；后续 Windows 源码验证进展见 [Windows 11 镜像流程验证](03-windows-image-validation.md)。

## 已实现

- 中文 Qt 桌面：来源与工作目录选择、后台扫描进度/取消、搜索和类型筛选、批量勾选、预览、导出报告、重新打开扫描记录。
- raw NTFS、MBR/GPT 和扩展分区探测；删除目录额外遍历，回收站 `$I/$R` 残留记录关联。
- Windows 有盘符 NTFS 卷的只读枚举/读取入口、管理员重启、磁盘身份复核、源盘与工作/目标位置隔离。
- 文本和常见图片预览、Office 文字摘录；预览不执行宏或打开外部程序。
- 保留中文与目录的导出，处理重名、危险路径、容量不足、读取异常和取消，保留逐文件状态及摘要。
- Windows x64 便携包，含 Python 3.13.12、PySide6/Shiboken 6.8.3、TSK 4.15.0、启动器、环境检查、使用说明、自己的应用源码及第三方对应源码/许可。

## 实际验证证据

| 检查 | 本轮结果 | 边界 |
| --- | --- | --- |
| 完整自动测试 | 98 项通过，无跳过 | macOS 开发主机，Windows 卷 API 使用受控契约输入 |
| 实际 Qt 与 TSK 流程 | 分区识别→扫描→筛选→预览→保存→重开记录通过 | NIST 镜像，Qt 离屏渲染 |
| 导出字节核对 | `Bunda.txt` 4,296 字节，与指定参考摘要一致 | 参考来自删除后镜像的文档指定区域，非独立删除前原件 |
| 固定依赖归档 | 8 个二进制/对应源码归档长度及 SHA-256 全部匹配 | 构建输入核对 |
| Windows PE 检查 | 147 个 PE 文件位数及导入库静态检查通过 | 不是 Windows 执行或完整动态依赖模拟 |
| 包文件清单 | 391 个文件长度及 SHA-256 核对通过 | 完整性核对不证明恢复效果 |

原始开发证据保存在开发机的 `artifacts/`（不提交到 Git，不装入用户测试包）。公开仓库包含复现工具与固定输入摘要，可按[开发说明](../development.md)重新生成：

- `tests-v0.2.log`：98 项自动测试输出。
- `desktop-demo-03/`：实际窗口截图、扫描记录、恢复报告、独立重新读取导出字节的验证报告。
- `windows-pe-audit.json`：PE 位数及导入依赖审计。
- `windows-build.json`：便携包路径、字节数及 SHA-256。

NIST 镜像 SHA-256：`c863ccad01804b840a6dfa623a94996ca876e15ded41c6c0d8ae148620eb6493`。NTFS 起始扇区为 61，每扇区 512 字节。导出内容 SHA-256：`be9f9a4f99b5ce961d5c2759fff20543d0ffbedbd27be0c5157174bb46be8b85`。扫描及导出前后源镜像复核通过。[来源及解释](../../tests/integration/fixture-source.md)

## 交付

[GitHub 预发布版](https://github.com/xiaomengsuifeng/data-recovery/releases/tag/v0.2.0)提供 `ShiHui-0.2.0-windows-x64.zip`，118,891,506 字节，SHA-256：

```text
1bd2680a90f83ce15e1245a53d1ec0e85508c36498b1d06c847e7d128b3d9305
```

完整解压后双击 `Start-ShiHui.cmd`；`Check-Package.cmd` 在 Windows 上核对文件并实际检查 Qt/TSK 启动。包内 `sources/` 提供 Qt/PySide6/Shiboken/TSK 对应源码，`src/` 提供本项目应用源码。详见[使用说明](../desktop-guide.md)及[构建说明](../development.md)。

## 真机验收仍需完成

Windows 11 的运行库实际加载、中文路径、DPI、UAC、原始卷访问、源/目标磁盘识别及异常断开；Windows Shell 实际清空回收站后，按删除前原件逐文件验证结果。隔离样本脚本随包提供，测试不要求删除用户的重要文件。

尚无深度内容扫描、其他文件系统、BitLocker/EFS 解密、坏盘采集、断点续扫或 TRIM 逆转。没有竞品对照实测，不据此声称普遍恢复率或算法领先。
