"""Source launcher used with the bundled Windows embeddable Python runtime."""
import os
from pathlib import Path
import sys
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
os.environ["RECOVERY_TSK_BIN"] = str(ROOT / "vendor" / "tsk" / "bin")

try:
    from recovery_desktop.app import main
    raise SystemExit(main())
except Exception:
    import ctypes
    message = "拾回启动失败。请完整解压测试包，保留 runtime、src、vendor 文件夹。\n\n" + traceback.format_exc()[-3500:]
    ctypes.windll.user32.MessageBoxW(None, message, "拾回 · 启动错误", 0x10)
