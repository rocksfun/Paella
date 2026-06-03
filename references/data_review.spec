import os

# Get the directory where the spec file is located
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
# The project root is one level up
PROJECT_ROOT = os.path.dirname(SPEC_DIR)

block_cipher = None

# Reference folder names
REF_DIR_NAME = 'references'
REF_ABS_PATH = os.path.join(PROJECT_ROOT, REF_DIR_NAME)

# Gather files from the references directory, excluding system_config.txt
added_files = [
    (os.path.join(REF_ABS_PATH, f), REF_DIR_NAME) 
    for f in os.listdir(REF_ABS_PATH) 
    if f != 'system_config.txt' and os.path.isfile(os.path.join(REF_ABS_PATH, f))
]

a = Analysis(
    [os.path.join(PROJECT_ROOT, 'helper_functions', 'AUX_data_review_viewer.py')],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=added_files,
    hiddenimports=[
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'matplotlib',
        'seaborn',
        'polars',
        'pandas',
        'numpy',
        'helper_functions.META_sample_selection',
        'helper_functions.SYSTEM_pull_config_io',
        'helper_functions.DATA_posthoc_frequency_analysis',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Paella_DataReview',
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
    icon=[os.path.join(PROJECT_ROOT, 'references', 'travera_logo.ico')],
)
