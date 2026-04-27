# -*- mode: python ; coding: utf-8 -*-

import os

from PyInstaller.utils.hooks import get_module_file_attribute


wx_dir = os.path.dirname(get_module_file_attribute('wx'))
webview2_loader = os.path.join(wx_dir, 'WebView2Loader.dll')


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[(webview2_loader, 'wx')],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='猫耳FM',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
