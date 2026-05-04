# -*- mode: python ; coding: utf-8 -*-
import sys, os
from pathlib import Path

block_cipher = None

# tkinterdnd2 data files
import tkinterdnd2
tkdnd_dir = str(Path(tkinterdnd2.__file__).parent)

a = Analysis(
    ['dj_app.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        (tkdnd_dir, 'tkinterdnd2'),
    ],
    hiddenimports=[
        'tkinterdnd2',
        'miniaudio',
        'sounddevice',
        'soundfile',
        'rtmidi',
        'numpy',
        'cffi',
        '_cffi_backend',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pydub', 'ffmpeg'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DJApp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DJApp',
)

app = BUNDLE(
    coll,
    name='LuchaPinchadiscos.app',
    icon='LuchaPinchadiscos.icns',
    bundle_identifier='com.luchapinchadiscos.lp',
    info_plist={
        'NSHighResolutionCapable': True,
        'NSMicrophoneUsageDescription': 'MIDI audio output',
        'LSMinimumSystemVersion': '11.0',
    },
)
