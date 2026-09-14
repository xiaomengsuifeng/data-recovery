# 拾回 0.2.0 开发说明

核心支持 NTFS 镜像及 Windows NTFS 卷，桌面采用 PySide6 / Qt Widgets，读取由独立 TSK 4.15.0 CLI 提供。核心 Python 代码无第三方运行依赖；桌面增加 PySide6-Essentials。自己的代码采用 MIT，第三方组件单独按其许可使用，见[第三方声明](../THIRD_PARTY_NOTICES.md)。

## 本机桌面开发

使用 Python 3.11–3.13，并准备同版本的 `fls` 和 `icat`：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[desktop,audit]'
PYTHONPATH=src .venv/bin/python -B -m recovery_desktop --tsk-bin /absolute/path/tsk/bin
```

Windows 开发环境使用 `.venv\Scripts\python.exe`。便携包用户直接运行 `Start-ShiHui.cmd`，无需以上步骤。`RECOVERY_TSK_BIN` 可指定工具目录；打包启动器自动设置它。

`src/recovery_core` 包含分区探测、扫描/导出、回收站关联、只读卷访问、预览及原件校验。`src/recovery_desktop` 包含实际窗口、可筛选的候选模型和 QThread 任务。工作线程报告进度，合作式取消传播到哈希循环和子进程执行器。

## CLI

CLI 的扫描命令限定 raw 镜像，真实卷扫描通过桌面入口提供。

```sh
export PYTHONPATH="$PWD/src"
python3 -B -m recovery_core doctor --tsk-bin /path/to/tsk/bin
python3 -B -m recovery_core scan /path/to/disk.img --offset 2048 --output /path/to/scan-01 --tsk-bin /path/to/tsk/bin
python3 -B -m recovery_core recover /path/to/scan-01 --destination /path/to/recovered-01 --tsk-bin /path/to/tsk/bin
python3 -B -m recovery_core verify /path/to/recovered-01 --manifest /path/to/targets.manifest.json --output /path/to/verification-01.json
```

偏移必须替换为实际 NTFS 起始扇区，分区镜像通常为 0。扇区大小默认 512，可使用 `--sector-size`。桌面自动读取布局。每次扫描、导出需要新目录且父目录已存在，防止覆盖已有结果。

`recover --id <候选ID>` 可以重复，限定目标。CLI 默认单文件上限 1 GiB，可用 `--max-file-bytes` 改变；桌面不采用该默认上限，没有收费额度。实际导出受空间、源文件状态及目标文件系统约束。

## 报告与路径

- `session.json`：来源、几何参数、TSK 版本、候选记录、名称证据、警告与局限。
- `recovery.json`：每个输出路径、字节数、SHA-256、导出/部分/失败/跳过状态，以及来源复核状态。
- `verification.json`：重新读取导出字节，按独立原件清单一对一匹配，分别统计内容、名称、目录。

输出为 `files/<安全处理后的原目录>/<文件名>`；保留中文，重名用候选 ID 和必要的递增后缀避让。危险、保留或过长的路径片段被编码或缩短，原始记录保存在报告。不把镜像中的绝对路径当作宿主写入路径。没有 `$I` 证据的 `$R` 内容仍可导出，但原名记为未知。

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

预览最多读取 32 MiB；Office ZIP 的 XML 每项不超过 2 MiB，最多 12 项，拒绝实体声明。图片限制解码内存与像素数。导出前检查可用空间，实际写入失败按逐文件异常保留结果。取消导出、哈希或最终来源核对时保留报告，重试使用新目录；尚无断点续扫。

## 可复现验证

```sh
PYTHONPATH=src QT_QPA_PLATFORM=offscreen .venv/bin/python -B -m unittest discover -s tests -v
.venv/bin/python -B tools/run_nist_demo.py --fixture-cache artifacts/fixtures --output artifacts/nist-new --tsk-bin /path/to/tsk/bin
QT_QPA_PLATFORM=offscreen .venv/bin/python -B tools/run_desktop_demo.py --output artifacts/desktop-new --tsk-bin /path/to/tsk/bin
```

没有桌面依赖时 Qt 测试会跳过，完整验收必须安装 `[desktop]`。当前完整套件为 98 项。`run_desktop_demo.py` 操作实际 Qt 窗口和工作线程，输出截图、扫描/恢复/校验报告；离屏渲染适用于开发 CI，不等于 Windows 真机测试。

演示使用 NIST DFR-01 镜像，导出 4,296 字节的 `Bunda.txt`，与文档指定的删除后磁盘区域核对。这不是独立删除前原件，也不是普遍恢复率证据。[样本说明](../tests/integration/fixture-source.md)

[Windows 隔离样本脚本](windows-testing.md)保存删除前原件，按 Windows Shell 操作生成清空回收站样本；脚本与直接卷入口尚待 Windows 11 真机执行。

## 构建 Windows 便携包

可在 macOS、Linux 或 Windows 组装，无需交叉编译；必须使用锁定的 Windows 归档，不能把主机的虚拟环境复制进去。

```sh
python3 tools/fetch_windows_runtime.py
python3 tools/build_windows_bundle.py
.venv/bin/python tools/audit_windows_pe.py dist/ShiHui-0.2.0-windows-x64 --output artifacts/windows-pe-audit.json
python3 dist/ShiHui-0.2.0-windows-x64/check_package.py
```

`tools/runtime-lock.json` 记录版本、来源、长度、SHA-256、选用模块和对应源码。下载器只下载构建依赖，软件恢复过程中不联网。错误归档或 `.incomplete` 文件会保留并报错，检查后移走该文件再重试。

离线构建器核对全部归档，创建新的 `dist` 目录与 ZIP，加入启动器、应用源码、使用说明、隔离样本脚本、第三方对应源码/许可和逐文件清单。不含测试镜像、恢复数据、开发路径或虚拟环境。输出已存在时用 `--output` 指定新名称。

`audit_windows_pe.py` 需 `[audit]` 开发依赖，静态核对 PE 位数及导入库。Python/Qt 是 x64，TSK 是单独的 x86 进程，运行库分开放置；此检查不能证明 Windows 动态加载行为。`check_package.py` 在非 Windows 仅核对清单，在 Windows 还实际启动 Qt 和 TSK。

尚未签名，也未通过 Windows 实机验收；不要把静态打包和 macOS 桌面测试记为 Windows 已验证。
