"""Read-only package integrity and Qt/TSK startup smoke check."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'src'))


def main():
    manifest=json.loads((ROOT/'package-manifest.json').read_text(encoding='utf-8'))
    errors=[]
    for item in manifest['files']:
        path=ROOT/item['path']
        if not path.resolve().is_relative_to(ROOT.resolve()):raise RuntimeError('Unsafe manifest path')
        if not path.is_file():errors.append(item['path']+': missing');continue
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
        if path.stat().st_size!=item['size'] or digest.hexdigest()!=item['sha256']:
            errors.append(item['path']+': changed')
    if errors:
        print('\n'.join(errors));return 2
    if os.name=='nt':
        from recovery_core.tsk import Tsk
        backend=Tsk(ROOT/'vendor/tsk/bin')
        print(backend.versions)
        os.environ['QT_QPA_PLATFORM']='offscreen'
        from PySide6.QtWidgets import QApplication
        from recovery_desktop.app import RecoveryWindow
        app=QApplication([])
        window=RecoveryWindow(ROOT/'vendor/tsk/bin');app.processEvents();window.close()
        print('Qt startup OK')
    print('Package files verified:',len(manifest['files']))
    return 0


if __name__=='__main__':raise SystemExit(main())
