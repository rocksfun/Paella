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

# Add pypump_routines directory
routines_abs_path = os.path.join(REF_ABS_PATH, 'pypump_routines')
routines_dest_path = os.path.join(REF_DIR_NAME, 'pypump_routines')
added_files.append((routines_abs_path, routines_dest_path))

a = Analysis(
    [os.path.join(PROJECT_ROOT, 'helper_functions', 'AUX_utilities_UI.py')],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=added_files,
    hiddenimports=[
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'PySide6.QtOpenGLWidgets',
        'pyqtgraph',
        'numpy',
        'cv2',
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
        'helper_functions.DATA_realtime_frequency_analysis',
        'helper_functions.DATA_posthoc_frequency_analysis',
        'helper_functions.SMR_frequency_processing_functions',
        'helper_functions.AUX_frequency_binary_viewer',
        'helper_functions.DATA_batch_frequency_processor',
        'helper_functions.AUX_volume_images_viewer',
        'helper_functions.AUX_hdf5_image_viewer',
        'helper_functions.AUX_data_review_viewer',
        'matplotlib',
        'seaborn',
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
    name='Paella_utilities',
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
