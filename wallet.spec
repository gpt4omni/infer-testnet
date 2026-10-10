# PyInstaller spec for the Infer desktop wallet (Windows exe + Linux bundle).
# Usage: pyinstaller wallet.spec   (Python with tkinter + requirements installed)
# Hidden imports cover function-level imports PyInstaller's scanner can miss.

block_cipher = None

a = Analysis(
    ['wallet.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'coincurve', 'cryptography',
        'ledger', 'gateway', 'miner', 'money', 'eval', 'pq', 'gossip', 'verify',
        'canary', 'routing',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['test', 'unittest'],
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
    name='infer-wallet',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed app: no console on Windows
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
    upx=False,
    upx_exclude=[],
    name='infer-wallet',
)
