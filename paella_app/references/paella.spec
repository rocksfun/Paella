# PyInstaller spec — produces ../PaellaApp/ folder (Windows: copy entire folder to lab PC)
# Build: cd paella_app && python helper_functions/package_paella.py

import os
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
PROJECT_ROOT = os.path.dirname(SPEC_DIR)
REPO_ROOT = os.path.dirname(PROJECT_ROOT)

REF_DIR_NAME = 'references'
REF_ABS_PATH = os.path.join(PROJECT_ROOT, REF_DIR_NAME)

added_files = [
    (os.path.join(REF_ABS_PATH, f), REF_DIR_NAME)
    for f in os.listdir(REF_ABS_PATH)
    if f != 'system_config.txt' and os.path.isfile(os.path.join(REF_ABS_PATH, f))
]

routines_abs_path = os.path.join(REF_ABS_PATH, 'pypump_routines')
added_files.append((routines_abs_path, os.path.join(REF_DIR_NAME, 'pypump_routines')))

try:
    added_files += copy_metadata('nitypes')
    added_files += copy_metadata('nidaqmx')
except Exception:
    pass

# Bundle ALL PySide6 / shiboken6 DLLs and plugins (fixes QtWidgets DLL load failed)
pyside6_datas, pyside6_binaries, pyside6_hidden = collect_all('PySide6')
shiboken_datas, shiboken_binaries, shiboken_hidden = collect_all('shiboken6')

added_files += pyside6_datas + shiboken_datas
qt_binaries = pyside6_binaries + shiboken_binaries

paella_remote_hidden = collect_submodules('helper_functions.paella_remote')

a = Analysis(
    [os.path.join(PROJECT_ROOT, 'main_gui.py')],
    pathex=[PROJECT_ROOT],
    binaries=qt_binaries,
    datas=added_files,
    hiddenimports=[
        'pyqtgraph',
        'numpy',
        'cv2',
        'nidaqmx',
        'nidaqmx.system',
        'nidaqmx.constants',
        'nidaqmx.errors',
        'nidaqmx._lib',
        'nitypes',
        'hightime',
        'packaging',
        'polars',
        'scipy',
        'scipy.optimize',
        'scipy.stats',
        'scipy.signal',
        'sklearn',
        'sklearn.covariance',
        'OpenGL',
        'OpenGL.GL',
        'fastapi',
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'helper_functions.DATA_realtime_frequency_analysis',
        'helper_functions.DATA_posthoc_frequency_analysis',
        'helper_functions.AUX_frequency_binary_viewer',
    ] + pyside6_hidden + shiboken_hidden + paella_remote_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(PROJECT_ROOT, 'pyi_rth_pyside6_dll.py')],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Paella',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(PROJECT_ROOT, 'references', 'travera_logo.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='PaellaApp',
)
