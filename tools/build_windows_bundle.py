#!/usr/bin/env python3
"""Assemble a Windows portable distribution without cross-compiling Python.

All runtime archives must be downloaded separately and match runtime-lock.json.
This builder is offline, excludes user data, and creates a new package directory.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for data in iter(lambda:file.read(1024*1024),b''):digest.update(data)
    return digest.hexdigest()


def unpack(archive: Path, target: Path, prefix='', include=None):
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            name = info.filename
            if prefix:
                if not name.startswith(prefix):continue
                name=name[len(prefix):]
            if not name:continue
            if include and not any(fnmatch.fnmatchcase(name, pattern) for pattern in include):continue
            parts=PurePosixPath(name).parts
            if name.startswith('/') or '\\' in name or ':' in name or '..' in parts or info.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError('Unsafe archive member')
            path=target.joinpath(*parts)
            if info.is_dir():path.mkdir(parents=True,exist_ok=True);continue
            path.parent.mkdir(parents=True,exist_ok=True)
            with z.open(info) as source,path.open('xb') as dest:shutil.copyfileobj(source,dest)


def build(cache: Path, output: Path) -> Path:
    zip_path = output.with_name(output.name + '.zip')
    lock=json.loads((ROOT/'tools/runtime-lock.json').read_text())
    for item in lock['archives']:
        path=cache/item['filename']
        if path.stat().st_size != item['size'] or sha(path) != item['sha256']:
            raise ValueError(f"Runtime archive hash mismatch: {item['filename']}")
    if output.exists() or zip_path.exists():raise ValueError('Package output must be new')
    output.mkdir(parents=True)
    for item in lock['archives']:
        target=output/item['target']
        target.mkdir(parents=True,exist_ok=True)
        if item.get('mode') == 'source':
            shutil.copy2(cache/item['filename'],target/item['filename'])
            # Make notices readable without requiring users to unpack source.
            with tarfile.open(cache/item['filename']) as source:
                for member in source:
                    if not member.isfile():continue
                    name=PurePosixPath(member.name)
                    if '..' in name.parts or name.is_absolute():raise ValueError('Unsafe source member')
                    if ('LICENSES' in name.parts or name.name.lower().startswith(('license','copying','copyright','notice'))
                            or name.name == 'qt_attribution.json'):
                        if member.size > 2*1024*1024:raise ValueError('Unexpected license file size')
                        dest=output/'licenses'/Path(*name.parts)
                        dest.parent.mkdir(parents=True,exist_ok=True)
                        with source.extractfile(member) as src,dest.open('xb') as dst:shutil.copyfileobj(src,dst)
        else:
            unpack(cache/item['filename'],target,item.get('prefix',''),item.get('include'))
    runtime=output/'runtime'
    (runtime/'python313._pth').write_text('python313.zip\n.\nLib/site-packages\n../src\n',encoding='ascii')
    shutil.copytree(ROOT/'src',output/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info'))
    for path in ('LICENSE','THIRD_PARTY_NOTICES.md'):
        shutil.copy2(ROOT/path,output/path)
    for path in ('launch.pyw','Install-Shortcut.ps1'):
        shutil.copy2(ROOT/'tools/windows'/path,output/path)
    shutil.copytree(ROOT/'docs',output/'docs')
    (output/'tests/integration').mkdir(parents=True)
    shutil.copy2(ROOT/'tests/integration/fixture-source.md',output/'tests/integration/fixture-source.md')
    (output/'使用说明.md').write_text(
        '# 拾回使用说明\n\n双击 `Start-ShiHui.cmd` 启动。\n\n'
        '- [桌面操作、支持范围与故障处理](docs/desktop-guide.md)\n'
        '- [候选版交付与验收边界](docs/milestones/11-extended-software.md)\n'
        '- [Windows 验证工具使用方法](docs/windows-testing.md)\n', encoding='utf-8')
    shutil.copy2(ROOT/'tools/runtime-lock.json',output/'runtime-lock.json')
    (output/'validation').mkdir()
    shutil.copy2(ROOT/'tools/windows/New-RecoveryFixture.ps1',output/'validation/New-RecoveryFixture.ps1')
    shutil.copy2(ROOT/'tools/windows/Invoke-RecoveryValidation.ps1',output/'validation/Invoke-RecoveryValidation.ps1')
    shutil.copy2(ROOT/'tools/windows/Invoke-LiveVolumeValidation.ps1',output/'validation/Invoke-LiveVolumeValidation.ps1')
    shutil.copy2(ROOT/'tools/live_volume_validation.py',output/'validation/live_volume_validation.py')
    shutil.copy2(ROOT/'tools/run_desktop_acceptance.py',output/'validation/run_desktop_acceptance.py')
    shutil.copy2(ROOT/'tools/run_extended_acceptance.py',output/'validation/run_extended_acceptance.py')
    (output/'validation/windows-testing.md').write_text(
        '# Windows 验证工具\n\n[完整使用方法、样本范围和验收说明](../docs/windows-testing.md)\n',
        encoding='utf-8')
    (output/'Start-ShiHui.cmd').write_bytes(b'@echo off\r\nstart "" /D "%~dp0" "%~dp0runtime\\pythonw.exe" -B "%~dp0launch.pyw"\r\n')
    (output/'Create-Desktop-Shortcut.cmd').write_bytes(b'@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-Shortcut.ps1"\r\npause\r\n')
    (output/'Check-Package.cmd').write_bytes(b'@echo off\r\n"%~dp0runtime\\python.exe" -B "%~dp0check_package.py"\r\npause\r\n')
    shutil.copy2(ROOT/'tools/windows/check_package.py',output/'check_package.py')
    required=['runtime/pythonw.exe','runtime/python313.dll','runtime/Lib/site-packages/PySide6/QtWidgets.pyd',
              'runtime/Lib/site-packages/PySide6/plugins/platforms/qwindows.dll','vendor/tsk/bin/fls.exe','vendor/tsk/bin/icat.exe']
    for path in required:
        if not (output/path).is_file():raise ValueError('Missing package component: '+path)
    entries=[{'path':p.relative_to(output).as_posix(),'size':p.stat().st_size,'sha256':sha(p)} for p in sorted(output.rglob('*')) if p.is_file()]
    (output/'package-manifest.json').write_text(json.dumps({'schema_version':1,'version':lock['version'],'platform':'Windows 11 x64',
        'windows_execution_verified':False,'files':entries},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with zipfile.ZipFile(zip_path,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in sorted(output.rglob('*')):
            if path.is_file():archive.write(path,output.name+'/'+path.relative_to(output).as_posix())
    return zip_path


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',type=Path,default=ROOT/'artifacts/downloads')
    parser.add_argument('--output',type=Path,default=ROOT/'dist/ShiHui-0.3.0rc1-windows-x64')
    args=parser.parse_args()
    package=build(args.cache,args.output)
    print(json.dumps({'package':str(package),'bytes':package.stat().st_size,'sha256':sha(package)},indent=2))
