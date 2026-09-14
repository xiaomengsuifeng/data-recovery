# 开源恢复组件与源码审计

核查日期：2026-09-12。范围是官方文档、公开仓库和选定调用路径的静态阅读；**没有编译、运行恢复程序或比较恢复率**。GitHub HEAD 已锁定如下，实际下载通过 HTTPS API 完成，未执行仓库安装脚本。下列“建议”是工程判断，不是实测结论。

## 选型结论

建议采用“文件系统解析 + 通用 carving + 独立校验 + 专项重建”的多路径架构。PhotoRec 可作通用 carving 基线，TSK 可作文件系统读取基线；ddrescue 位于镜像采集层。Scalpel3 列入实验对照，libyal 用作补充解析与交叉检查，均不应直接等同于完整商用恢复引擎。

| 组件与已核对版本 | 可复用能力、接口与平台 | 明确边界与许可证 |
|---|---|---|
| PhotoRec / TestDisk，`40e1b8d7830c754203df08f74796d935401632b0` | 主要为 C、命令行程序；PhotoRec 提供格式 carving；TestDisk 提供分区/引导结构检查与部分文件系统恢复。Windows、Linux、macOS 等平台。 | PhotoRec 不能普遍恢复原目录；TestDisk 的“识别某文件系统”不等于该系统所有删除恢复功能。审读核心文件为 GPL-2.0-or-later。[项目](https://www.cgsecurity.org/wiki/TestDisk)、[源码许可](https://github.com/cgsecurity/testdisk/blob/40e1b8d7830c754203df08f74796d935401632b0/src/photorec.c#L7) |
| TSK，`c71437097f704ac149b60403d2a6ddc8c6a484f4` | C/C++ 库与 CLI；解析 NTFS、FAT/exFAT、APFS 等，枚举删除条目、读取属性与文件数据；Windows、Unix。 | 文件系统支持列表不是受损场景恢复率保证。NTFS、`tsk_recover` 为 CPL-1.0；`icat_lib.c` 为 IBM Public License；Java 数据模型为 Apache-2.0，另有 MIT/BSD/GPL 文件，**不能统称 MIT**。[能力](https://www.sleuthkit.org/sleuthkit/desc.php)、[分模块许可](https://github.com/sleuthkit/sleuthkit/blob/c71437097f704ac149b60403d2a6ddc8c6a484f4/licenses/README.md) |
| Scalpel3，`c627eb83ed19e69b37bf31f8c8d0e1b93108dd23` | 确有公开 C 源码；Linux/macOS，多线程候选重建、块映射和校验回调，可扩展专项格式。 | 部分验证器仍在开发；未验证 Windows 移植和我们的真实故障样本。GPL-3.0-or-later，另附集成说明，闭源集成需单独核对。[源码/状态](https://github.com/nolaforensix/scalpel3-release/tree/c627eb83ed19e69b37bf31f8c8d0e1b93108dd23)、[许可](https://github.com/nolaforensix/scalpel3-release/blob/c627eb83ed19e69b37bf31f8c8d0e1b93108dd23/LICENSE.md) |
| libfsntfs，`0d2d9d7282e4910eba6b9ff4933fe421a3f39c9a`；libfsapfs，`c5afe4df9fbbb2aaf778635e9f3ada3af58e4c80` | C 库、Python 绑定与工具，有 Windows/Unix 构建资料；只读访问，可补足解析/交叉核对。 | 官方均标 experimental。NTFS 不支持 EFS；APFS 不支持 snapshots、Fusion、T2 加密等。审读库及 info 工具文件头均为 LGPL-3.0-or-later，仓库同时放 GPL 与 LGPL 文本不能据此把工具一概标 GPL。[NTFS](https://github.com/libyal/libfsntfs/blob/0d2d9d7282e4910eba6b9ff4933fe421a3f39c9a/README)、[APFS](https://github.com/libyal/libfsapfs/blob/c5afe4df9fbbb2aaf778635e9f3ada3af58e4c80/README)、[工具文件头](https://github.com/libyal/libfsapfs/blob/c5afe4df9fbbb2aaf778635e9f3ada3af58e4c80/fsapfstools/fsapfsinfo.c#L8) |
| GNU ddrescue，官方手册 1.30 | CLI；优先读取可读区域，以 mapfile 保存区域状态和续读位置，供后续恢复使用。 | 解决介质读取/镜像，不负责目录和文件重建；Windows 原生打包不在本次验证范围。GPL-2.0-or-later。[手册](https://www.gnu.org/software/ddrescue/manual/ddrescue_manual.html)、[许可](https://www.gnu.org/software/ddrescue/ddrescue.html) |
| untrunc，`9d86ec9ef2ffed1bf8131abe80742c0574db52b6`；FFmpeg | untrunc 是 C++ 视频修复工具；用相似正常视频辅助修复截断 MP4/MOV 等。FFmpeg 可用于解析和完整解码校验。 | 视频容器修复与磁盘碎片恢复是不同任务。untrunc 文件头为 GPL-2.0-or-later；其 README 警示新 FFmpeg 内部结构兼容风险。FFmpeg 默认 LGPL-2.1-or-later，启用 GPL 部件会改变许可，必须锁定构建配置。[untrunc](https://github.com/anthwlock/untrunc/blob/9d86ec9ef2ffed1bf8131abe80742c0574db52b6/README.md)、[文件头](https://github.com/anthwlock/untrunc/blob/9d86ec9ef2ffed1bf8131abe80742c0574db52b6/src/main.cpp#L4)、[FFmpeg](https://www.ffmpeg.org/legal.html) |

## 三条已经阅读的源码路径

**1. PhotoRec：文件头命中 → 格式校验 → 保存结果。** `register_header_check_jpg()` 登记 JPEG 特征；`header_check_jpg()` 为新候选设置 `data_check_jpg`、`file_check_jpg` 回调；最终检查包含 JPEG 结构检查，并受 libjpeg 编译条件影响。`file_finish2()` 处理无效和截断结果；另有 `phbf.c` 的 `photorec_bf()`、`photorec_bf_frag()` 搜索路径。因此“给 PhotoRec 加校验/碎片尝试”本身不能算原创优势，需找具体失败样本。来源：[特征登记](https://github.com/cgsecurity/testdisk/blob/40e1b8d7830c754203df08f74796d935401632b0/src/file_jpg.c#L2772)、[回调设置](https://github.com/cgsecurity/testdisk/blob/40e1b8d7830c754203df08f74796d935401632b0/src/file_jpg.c#L1049)、[最终检查](https://github.com/cgsecurity/testdisk/blob/40e1b8d7830c754203df08f74796d935401632b0/src/file_jpg.c#L2442)、[结果处理](https://github.com/cgsecurity/testdisk/blob/40e1b8d7830c754203df08f74796d935401632b0/src/photorec.c#L781)、[碎片尝试](https://github.com/cgsecurity/testdisk/blob/40e1b8d7830c754203df08f74796d935401632b0/src/phbf.c#L540)。

**2. TSK：残留元数据 → 文件数据段 → 按路径导出。** `ntfs_inode_lookup()` 读取 MFT 并转换为统一元数据；`ntfs_make_data_run()` 解析可变长度 runlist，检查越界和损坏；`tsk_fs_file_read()` 转入属性读取。`tsk_recover` 默认遍历未分配目录项，`processFile()`/`writeFile()` 用文件遍历接口输出内容。可据此实现带文件名/目录的恢复分支；元数据已覆盖、引用失效或原块重用时仍需我们判定冲突与完整性。来源：[MFT](https://github.com/sleuthkit/sleuthkit/blob/c71437097f704ac149b60403d2a6ddc8c6a484f4/tsk/fs/ntfs.c#L3040)、[runlist](https://github.com/sleuthkit/sleuthkit/blob/c71437097f704ac149b60403d2a6ddc8c6a484f4/tsk/fs/ntfs.c#L597)、[读取](https://github.com/sleuthkit/sleuthkit/blob/c71437097f704ac149b60403d2a6ddc8c6a484f4/tsk/fs/fs_file.c#L522)、[导出](https://github.com/sleuthkit/sleuthkit/blob/c71437097f704ac149b60403d2a6ddc8c6a484f4/tools/autotools/tsk_recover.cpp#L387)。

**3. Scalpel3：块分类 → 候选拼接 → 校验驱动搜索。** `scalpelconf.c` 定义格式；`scalpel.h` 暴露 `BLOCKVALIDATOR`、`FILEVALIDATOR`、能查看物理块映射的 `CANDIDATEVALIDATOR` 和 `REASSEMBLYFUNC`。默认 `LR_reassembly()` 按候选块扩展、验证和回退；`reassembly_check_validation()` 调用文件验证器并裁剪有效长度。复杂格式仍需自定义重建。状态序列化、深拷贝、检查点响应和线程安全都是接入成本，不是加一个文件头配置便完成。来源：[格式接口](https://github.com/nolaforensix/scalpel3-release/blob/c627eb83ed19e69b37bf31f8c8d0e1b93108dd23/src/scalpelconf.c#L95)、[回调声明](https://github.com/nolaforensix/scalpel3-release/blob/c627eb83ed19e69b37bf31f8c8d0e1b93108dd23/src/scalpel.h#L738)、[搜索](https://github.com/nolaforensix/scalpel3-release/blob/c627eb83ed19e69b37bf31f8c8d0e1b93108dd23/src/reassembly.c#L80)、[校验调用](https://github.com/nolaforensix/scalpel3-release/blob/c627eb83ed19e69b37bf31f8c8d0e1b93108dd23/src/reassembly.c#L649)。

Scalpel3 的 [2026-08-24 v2 预印本](https://arxiv.org/abs/2608.20363v2) 报告了受控碎片场景实验。那是作者的实验，不是我们对消费级相机卡、NTFS 删除或商业工具的独立验证。当前应先复现，再决定能否采用；不能由论文标题推导出“已超过商业产品”。

## 我们需要自研什么

- 统一候选模型：原盘偏移/数据段、来源引擎、原始名称证据、缺失区域、冲突块、校验结果和版本；关联目录解析与 carving 的重复发现。
- 失败场景专项重建：利用文件系统残留、相机布局、容器索引和编码约束缩小拼接搜索；把错误拼接率作为硬指标。
- 产品可靠性：只读设备代理、目标盘检查、取消/续扫、故障隔离、预览资源限制、百万候选索引与可复查报告。
- 独立判定：结构有效、完整解码、部分可读分别展示；只有测试原件哈希相同才计“字节完全恢复”。验证器输出的 `validates` 不等于原文件同一性证明。

验证顺序：先用固定镜像比较 TSK + PhotoRec 基线；再加入 Scalpel3 对照；最后按失败类型加入自研模块做消融测试。每项报告哈希一致的完整文件数、错误拼接数、目录/名称准确率、时间与内存。只有稳定提升且不恶化误报，才把对应场景写进产品优势。

## 集成与分发决策

进程隔离适合稳定性和权限控制，但 **sidecar/子进程不自动豁免 GPL**：分发原程序、修改后的对应源码、许可文本仍各有义务；程序是否构成整体还涉及通信机制与语义。TSK 的 CPL/IPL 与 GPL 组件也不能未经审核就静态合并成一个可分发二进制。先保留独立上游程序与清晰文件接口，逐一记录实际链接模块和构建选项；若计划闭源，需在产品许可确定前核对组合方式。[GNU 对聚合与通信的说明](https://www.gnu.org/licenses/gpl-faq.en.html#MereAggregation)、[TSK 官方许可页](https://www.sleuthkit.org/sleuthkit/licenses.php)。
