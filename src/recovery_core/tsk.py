from __future__ import annotations

import os
import io
import locale
import math
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .common import RecoveryError
from .control import checkpoint, progress

ATTRIBUTE_ID = re.compile(r"^[0-9]+-128-[0-9]+$")
REGULAR_BODY_MODE = re.compile(r"^[r-]/r[rwxstST-]{9}$")
STDERR_LIMIT = 1024 * 1024
PIPE_CHUNK_BYTES = 64 * 1024


def _output_encoding() -> str:
    # Windows TSK uses setlocale(LC_ALL, "") and wide printf in text mode;
    # redirected listings use the native ANSI code page, not Python's UTF-8
    # mode. Unix TSK writes its internal UTF-8 directly. Never guess per record.
    return locale.getencoding() if os.name == "nt" else "utf-8"


def run_bounded(command: list[str], output, *, limit: int, timeout: float) -> None:
    """Copy at most limit stdout bytes and 1 MiB stderr, then reap the child.

    Both pipes are drained concurrently using threads because Windows anonymous
    pipes cannot be polled with select(). Only the stdout reader writes output;
    it is joined before this function returns or raises, so callers may safely
    close/hash the destination. TSK runs directly without a shell.
    """
    if (type(limit) is not int or limit < 0 or type(timeout) not in (int, float)
            or not math.isfinite(timeout) or timeout <= 0):
        raise RecoveryError("Invalid TSK output budget or timeout.")
    environment = dict(os.environ, LC_ALL="C", TZ="UTC")
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=environment, shell=False,
                               bufsize=0, **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    diagnostic = bytearray()
    failures: queue.Queue[BaseException] = queue.Queue(maxsize=1)
    failed = threading.Event()
    readers: list[threading.Thread] = []

    def copy_pipe(pipe, budget: int, *, is_stdout: bool) -> None:
        try:
            remaining = budget
            while not failed.is_set():
                # One extra byte distinguishes exact-budget EOF from overflow.
                block = pipe.read(min(PIPE_CHUNK_BYTES, remaining + 1))
                if not block:
                    break
                accepted = block[:remaining]
                if is_stdout:
                    pending = memoryview(accepted)
                    while pending:
                        written = output.write(pending)
                        if written is None or written <= 0 or written > len(pending):
                            raise OSError("Destination did not accept the TSK output bytes.")
                        pending = pending[written:]
                else:
                    diagnostic.extend(accepted)
                remaining -= len(accepted)
                if len(block) > len(accepted):
                    label = "output" if is_stdout else "diagnostic output"
                    raise RecoveryError(f"TSK {label} exceeded the configured size limit.")
        except BaseException as exc:
            try:
                failures.put_nowait(exc)
            except queue.Full:
                pass
            failed.set()
        finally:
            pipe.close()

    def stop_and_reap() -> None:
        # A full pipe is never waited on before terminating its writer. Retry
        # cleanup on a second Ctrl+C so no reader can outlive the output stream.
        failed.set()
        while True:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait()
                for reader in readers:
                    if reader.ident is not None:
                        reader.join()
                return
            except KeyboardInterrupt:
                continue

    try:
        for pipe, budget, is_stdout in ((process.stdout, limit, True),
                                       (process.stderr, STDERR_LIMIT, False)):
            reader = threading.Thread(target=copy_pipe, args=(pipe, budget),
                                      kwargs={"is_stdout": is_stdout},
                                      name=f"recovery-tsk-{'stdout' if is_stdout else 'stderr'}")
            readers.append(reader)
            reader.start()
        deadline = time.monotonic() + timeout
        while True:
            checkpoint()
            if failed.is_set():
                raise failures.get_nowait()
            if process.poll() is not None and all(not reader.is_alive() for reader in readers):
                break
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise RecoveryError(f"TSK timed out after {timeout:g} seconds.")
            failed.wait(min(0.025, remaining_time))
        # Readers have finished, including the final budget check and pipe close.
        detail = diagnostic[:8192].decode(_output_encoding(), errors="replace").strip()
        if process.returncode:
            raise RecoveryError(f"TSK exited with {process.returncode}: {detail}")
        if detail:
            raise RecoveryError(f"TSK reported a diagnostic: {detail}")
    finally:
        stop_and_reap()
        # Also close pipes for which thread startup failed.
        process.stdout.close()
        process.stderr.close()
        output.flush()


def parse_body(text: str) -> tuple[list[dict], list[str]]:
    candidates, warnings = [], []
    seen = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line:
            continue
        try:
            # The filename may itself contain '|'; fixed numeric suffix fields
            # are parsed from the right, never interpreted as a destination.
            _, remaining = line.split("|", 1)
            name, inode, mode, uid, gid, size, atime, mtime, ctime, crtime = remaining.rsplit("|", 9)
            int(uid); int(gid)
            for value in (atime, mtime, ctime, crtime):
                int(value)
            size_value = int(size)
            if size_value < 0 or size_value > (1 << 63) - 1:
                raise ValueError("invalid size")
        except ValueError:
            raise RecoveryError(f"Unrecognized fls body record at line {number}.") from None
        if name.endswith(" (deleted-realloc)"):
            warnings.append(f"Skipped reallocated metadata at line {number}.")
            continue
        if not name.endswith(" (deleted)"):
            warnings.append(f"Skipped entry without deletion evidence at line {number}.")
            continue
        name = name[:-len(" (deleted)")]
        # NTFS deleted names may have unknown name type '-' while metadata still
        # identifies a regular file ('-/r...'). Neither directory metadata nor a
        # contradictory directory/symlink name type is a regular-file candidate.
        if not ATTRIBUTE_ID.fullmatch(inode) or not REGULAR_BODY_MODE.fullmatch(mode):
            warnings.append(f"Skipped unsupported attribute/type at line {number}.")
            continue
        if ":" in name or any(ord(c) < 32 for c in name):
            warnings.append(f"Skipped named stream or control-character name at line {number}.")
            continue
        key = inode, name
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"inode": inode, "observed_path": name, "size": size_value,
                           "original_path": name, "path_evidence": "filesystem_entry",
                           "warnings": [], "kind": "file"})
    return candidates, warnings


class Tsk:
    def __init__(self, binary_directory: Path | None = None, timeout: float = 120):
        self.timeout = timeout
        self.executables = {}
        self.versions = {}
        for name in ("fls", "icat"):
            if binary_directory:
                binary = binary_directory / (name + (".exe" if os.name == "nt" else ""))
                executable = str(binary.resolve()) if binary.is_file() else None
            else:
                executable = shutil.which(name)
            if not executable:
                raise RecoveryError(f"Missing TSK tool '{name}'. Install The Sleuth Kit or use --tsk-bin.")
            self.executables[name] = executable
            with io.BytesIO() as output:
                run_bounded([executable, "-V"], output, limit=4096, timeout=10)
                output.seek(0)
                self.versions[name] = output.read().decode(_output_encoding(), errors="strict").strip()
        if self.versions["fls"] != self.versions["icat"]:
            raise RecoveryError("fls and icat versions differ; use tools from the same release.")

    def command(self, tool: str, image: Path, offset: int, sector_size: int) -> list[str]:
        return [self.executables[tool], "-i", "raw", "-f", "ntfs", "-b", str(sector_size),
                "-o", str(offset)]

    def _listing(self, image: Path, offset: int, sector_size: int, *, directories=False,
                 inode: str | None = None, prefix="/") -> str:
        command = self.command("fls", image, offset, sector_size)
        command += ["-r", "-d", "-D" if directories else "-F", "-m", prefix, str(image)]
        if inode is not None:
            if not re.fullmatch(r"[0-9]+", inode):
                raise RecoveryError("Invalid directory record.")
            command.append(inode)
        with io.BytesIO() as output:
            run_bounded(command, output, limit=32 * 1024 * 1024, timeout=self.timeout)
            output.seek(0)
            encoding = _output_encoding()
            try:
                text = output.read().decode(encoding, errors="strict")
            except UnicodeError as exc:
                raise RecoveryError(f"fls output is not valid {encoding}; refusing ambiguous names.") from exc
        return text

    def scan(self, image: Path, offset: int, sector_size: int) -> tuple[list[dict], list[str]]:
        progress("scan", message="查找已删除的文件记录")
        candidates, warnings = parse_body(self._listing(image, offset, sector_size))
        queue = self._directories(self._listing(image, offset, sector_size, directories=True))
        visited = set()
        known = {(item["inode"], item["observed_path"]) for item in candidates}
        while queue and len(visited) < 2000:
            checkpoint()
            inode, path = queue.pop(0)
            if inode in visited:
                continue
            visited.add(inode)
            progress("directories", len(visited), 0, path)
            try:
                listing = self._listing(image, offset, sector_size, inode=inode, prefix=path.rstrip("/") + "/")
                children, notices = parse_body(listing)
                warnings.extend(notices)
                for item in children:
                    key = item["inode"], item["observed_path"]
                    if key not in known:
                        known.add(key)
                        candidates.append(item)
                queue.extend(self._directories(self._listing(image, offset, sector_size, directories=True,
                                                              inode=inode, prefix=path.rstrip("/") + "/")))
                if len(candidates) > 100000:
                    return candidates, warnings + ["Candidate limit reached during directory traversal."]
            except RecoveryError as exc:
                warnings.append(f"Deleted directory {inode} could not be fully traversed: {exc}")
        if queue:
            warnings.append("Deleted directory traversal reached its 2,000-record limit; some paths remain unvisited.")
        return candidates, warnings

    @staticmethod
    def _directories(text: str) -> list[tuple[str, str]]:
        found = []
        for line in text.splitlines():
            try:
                _, rest = line.split("|", 1)
                name, inode, mode, *_ = rest.rsplit("|", 9)
            except ValueError:
                continue
            if not name.endswith(" (deleted)") or not re.fullmatch(r"[d-]/d[rwxstST-]{9}", mode):
                continue
            if not re.fullmatch(r"[0-9]+(?:-144-[0-9]+)?", inode):
                continue
            path = name[:-len(" (deleted)")]
            if any(ord(c) < 32 for c in path) or any(p in (".", "..") for p in path.split("/")):
                continue
            found.append((inode.split("-")[0], path))
        return found

    def extract(self, image: Path, offset: int, sector_size: int, inode: str,
                output, limit: int) -> None:
        if not ATTRIBUTE_ID.fullmatch(inode):
            raise RecoveryError("Invalid or unsupported NTFS data attribute identifier.")
        command = self.command("icat", image, offset, sector_size)
        command += ["-r", str(image), inode]
        run_bounded(command, output, limit=limit, timeout=self.timeout)

    def extract_bitmap(self, image: Path, offset: int, sector_size: int, output, limit: int) -> None:
        # A fixed NTFS metadata record, never a user-controlled candidate ID.
        command = self.command("icat", image, offset, sector_size) + [str(image), "6"]
        run_bounded(command, output, limit=limit, timeout=self.timeout)

    def extract_log_metadata(self, image: Path, offset: int, sector_size: int, record: int, output, limit: int) -> None:
        if type(record) is not int or record not in (0, 2):
            raise RecoveryError("Only the fixed $MFT and $LogFile records are supported.")
        command = self.command("icat", image, offset, sector_size) + [str(image), str(record)]
        run_bounded(command, output, limit=limit, timeout=self.timeout)
