# PyInstaller spec — Paella Central Dashboard (operator PC only)
# Build: cd paella_dashboard && pyinstaller --noconfirm paella_dashboard.spec

import os

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
DASHBOARD_ROOT = SPEC_DIR

a = Analysis(
    [os.path.join(DASHBOARD_ROOT, 'run_dashboard.py')],
    pathex=[DASHBOARD_ROOT],
    binaries=[],
    datas=[
        (os.path.join(DASHBOARD_ROOT, 'static'), 'static'),
        (os.path.join(DASHBOARD_ROOT, 'protocol'), 'protocol'),
    ],
    hiddenimports=['uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PaellaDashboard',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PaellaDashboard',
)
