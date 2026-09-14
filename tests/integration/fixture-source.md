# NTFS 集成测试样本来源

取得日期：2026-09-12。样本是 NIST CFReDS 官方发布的 **DFR-01 NTFS 元数据删除恢复测试镜像**，由真实 NTFS 文件系统的受控创建／删除流程制成；不是本项目手工拼接的 MFT 字节，也不是我们在当前 Windows 或用户原盘上做的删除实验。

## 来源与测试用途

- [NIST DFR 镜像索引](https://cfreds-archive.nist.gov/dfr-test-images.html)：DFR-01 NTFS，单个非碎片删除文件。
- [完整压缩镜像](https://cfreds-archive.nist.gov/dfr-images/dfr-01-ntfs.dd.bz2)。
- [官方创建／布局文档](https://cfreds-archive.nist.gov/dfr-images/setup-july-10-2012.pdf)：PDF 第 185–186 页（ntfs-01）；第 186 页后半开始是另一个 recycle 用例，不可混用其偏移。
- [NIST 对 CFReDS 用途的说明](https://www.nist.gov/itl/csd/secure-systems-and-applications/computer-forensics-tool-testing-program-cftt/cfreds)明确包含软件工具验证。我们按该公开测试用途使用，不据此宣称所有 CFReDS 第三方内容都具有同一开源许可证。大镜像和 PDF 下载到临时目录，不随项目分发。

这是故障数据和文件内容经过控制的公开测试案例，不代表真实用户盘的复杂度，也不用于证明 carving、现代 Windows 全兼容或商业竞品优劣。

## 固定字节与布局

| 对象 | 大小／值 | SHA-256 |
|---|---|---|
| `dfr-01-ntfs.dd.bz2` | 2,257,102 bytes，约 2.15 MiB | `9625d8946bc7712953d0ca7e73e4eaeff26381006951d3f7603d1797b50d19e0` |
| 原样解压的 `dfr-01-ntfs.dd` | 1,073,742,336 bytes，即 1 GiB + 512 bytes | `c863ccad01804b840a6dfa623a94996ca876e15ded41c6c0d8ae148620eb6493` |
| `setup-july-10-2012.pdf` | 290 页 | `86b5a888e60f1127ac77e57929dd64bd4346af882f904ddd45ba7d90afbc803e` |
| NTFS 分区起点 | 61 sectors × 512 = **31,232 bytes** | 不适用 |
| NTFS 分区大小 | 586,881 sectors；末 sector 586,941 | 不适用 |
| 逻辑扇区／簇 | 512 bytes／4,096 bytes | 不适用 |

这些 SHA-256 是本次从 HTTPS 官方地址取得文件后自行计算的版本固定值，不是 NIST 提供或签名的摘要。原始 dd 包含分区外区域，所以总大小不是约 287 MiB 的分区大小。为保持来源与哈希一致，我们保留整个 1 GiB 镜像，未裁切、扩容或改写。

本机文件：`/tmp/data-recovery-fixtures/dfr-01-ntfs.dd`。macOS 解析后的路径是 `/private/tmp/data-recovery-fixtures/dfr-01-ntfs.dd`，两者为同一文件。

## 删除目标与断言依据

官方布局文档记录删除目标 `Bunda.txt`，长度 **4,296 bytes**，无覆盖。文件内容来自磁盘绝对 sector **107,005–107,012** 的八个整扇区，另加 sector **107,013** 的前 200 bytes。因此直接 extent 读取为：

```python
with open(image, "rb") as source:
    source.seek(107005 * 512)
    expected = source.read(4296)
```

本次从上述范围计算的 SHA-256 为 `be9f9a4f99b5ce961d5c2759fff20543d0ffbedbd27be0c5157174bb46be8b85`。可读标记包括 `DFR`、`File Bunda.txt path root` 和末段 `Tail Bunda.txt`。它可以验证 `icat` 与基于文档 extent 的读取一致，**不能作为另行取得的删除前原件哈希**；本次未取得独立原件文件。MFT 直接只读检查定位到记录 65、序号 2、未分配标志。

适合断言：扫描识别此已删除目标；保留名称和长度；提取字节与官方布局指定的镜像区间一致；提取前后镜像摘要不变。不能断言：一般 NTFS 删除恢复率、所有损坏情况、独立原件 bit-exact 证明。

## 获取和验证

```sh
python3 tools/fetch_test_fixture.py
```

脚本使用系统 `curl`（保持 TLS 验证）和 Python 标准库，下载约 2.15 MiB，展开到 `/tmp/data-recovery-fixtures`，需要约 1.01 GiB 磁盘空间。它校验压缩包及镜像的固定大小／哈希，再核对分区、NTFS 引导标记及已记录 extent。已有错误缓存会被拒绝，不会被静默覆盖；需要删除错误缓存或换一个空目标目录后重试。`--destination DIR` 可指定其他目录。

获取脚本已在本机通过空目录下载／解压／双层哈希／布局核验、现有缓存复核，并验证损坏缓存会被拒绝；额外的获取测试副本已经清理。

## 已完成的 Sleuth Kit 实测

2026-09-12 在当前 macOS 上运行 **The Sleuth Kit 4.15.0**，不是模拟 CLI 输出。本机临时工具目录为 `/tmp/data-recovery-tsk/bin`，内含 `fls`、`icat`、`mmls`、`fsstat`、`istat`。它没有安装进全局 PATH；项目若支持 `--tsk-dir`，传入该目录即可。

```sh
/tmp/data-recovery-tsk/bin/fls -V
/tmp/data-recovery-tsk/bin/mmls /tmp/data-recovery-fixtures/dfr-01-ntfs.dd
/tmp/data-recovery-tsk/bin/fls -r -d -p -l -o 61 /tmp/data-recovery-fixtures/dfr-01-ntfs.dd
/tmp/data-recovery-tsk/bin/istat -o 61 /tmp/data-recovery-fixtures/dfr-01-ntfs.dd 65
/tmp/data-recovery-tsk/bin/icat -r -o 61 /tmp/data-recovery-fixtures/dfr-01-ntfs.dd 65-128-2 > /tmp/data-recovery-fixtures/Bunda.recovered.txt
```

观察与核验结果：

- `fls -V` 返回 `The Sleuth Kit ver 4.15.0`；`mmls` 报告分区起点 61、长度 586,881 sectors，与官方布局吻合。
- `fls` 目标行的类型是 `-/r *`，地址 **`65-128-2`**，名称 `Bunda.txt`，长度 4,296。类型前半为未知并不代表没有可读取的文件元数据；不能只接收 `r/r` 而漏掉这条删除记录。
- `istat 65` 显示序号 2、未分配文件、父目录 MFT 5，以及非驻留 `$DATA (128-2)`：大小／已初始化大小均为 4,296，簇为 13,368、13,369。
- `icat -r -o 61 ... 65-128-2` 退出码 0，stderr 为空，输出 4,296 bytes；逐字节对比 `107005 * 512` 起的文档 extent **完全相同**，输出 SHA-256 为前述 `be9f9a4f…46be8b85`。
- 在该次 `icat` 前后分别读取完整镜像计算 SHA-256，均为 `c863ccad…0eb6493`，确认此次提取没有改变输入字节。输出保存为 `/tmp/data-recovery-fixtures/Bunda.recovered.txt`。
- `fls -d` 还列出地址 0 的 `Castor.txt` 残留项及若干 0-byte orphan；因此集成测试应断言目标存在及其导出内容，而非要求删除列表总数恰好为 1。

上述内容比对的基准是官方文档指明的**删除后镜像数据范围**。虽然它独立于 TSK 的文件系统解析路径，仍不是另行保存的删除前原件，不能扩写成“与独立原件哈希一致”。

## 本机临时 TSK 的取得方式

实际运行的是 **Homebrew 发布的 macOS Sonoma x86_64 二进制 bottle**，不是本轮从源码编译的程序。来源为 [Homebrew GHCR sleuthkit blob](https://ghcr.io/v2/homebrew/core/sleuthkit/blobs/sha256:e079c6a173f523658c6d26f536899c198178c953c76ccf5df0b41d646ccde891)，版本来自 Homebrew 的 4.15.0 bottle manifest。

| 本地取得的包 | 原始压缩包 SHA-256 |
|---|---|
| `sleuthkit-4.15.0.sonoma.bottle.tar.gz`，8,140,102 bytes | `e079c6a173f523658c6d26f536899c198178c953c76ccf5df0b41d646ccde891` |
| `afflib--3.7.22.sonoma.bottle.3.tar.gz` | `393511fd03c96d20bcd82e9cbb9280d03b741f5ffaa757cbcde4acfd51231566` |
| `libewf--20140816.sonoma.bottle.1.tar.gz` | `1b2e461e480ef015de567fd9f9916601cd5229755c6910b13c856c0ef0676d41` |

三个压缩包均核对过 Homebrew manifest 内公布的摘要。仅在 `/tmp/data-recovery-tsk` 解包和复制需要的可执行文件／动态库，然后用 `install_name_tool` 修补**临时副本**中的 Homebrew 占位路径，重新作本地 ad-hoc 签名。`afflib` 和 `libewf` 位于 `/tmp/data-recovery-tsk/lib`；SQLite、OpenSSL 使用机器已有的 `/usr/local/opt/sqlite/lib/libsqlite3.dylib` 与 `/usr/local/opt/openssl@3/lib/libcrypto.3.dylib`，其余为系统库。没有为此全局安装 TSK、Java、ICU 或改写现有库。

这是当前机器的临时实验运行方式，不是可分发安装包，也不保证换机器或清理 `/tmp` 后仍可用。上表哈希是**修改前包文件**的摘要，不是本地重定位后可执行文件的摘要。此前尝试的官方源码包下载没有用于构建；无用下载已经停止，不应将此次结果记为源码构建验证。
