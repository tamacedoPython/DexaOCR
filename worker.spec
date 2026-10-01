# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for DexaOCRWorker.

Build:
    pyinstaller worker.spec --noconfirm

Output: dist/DexaOCRWorker/DexaOCRWorker.exe  (onedir mode)

Place your production .env next to DexaOCRWorker.exe before deploying.
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules, collect_data_files

# ── PaddleOCR + PaddlePaddle ──────────────────────────────────────────────────
# These are large; collect_all gathers data files, binaries, and hidden imports.
paddle_d, paddle_b, paddle_h = collect_all("paddle")
paddleocr_d, paddleocr_b, paddleocr_h = collect_all("paddleocr")

# ── OpenCV ────────────────────────────────────────────────────────────────────
cv2_d, cv2_b, cv2_h = collect_all("cv2")

# ── pydicom data files (tag tables, transfer syntaxes, etc.) ──────────────────
pydicom_d = collect_data_files("pydicom")

# ── pydantic v2 ───────────────────────────────────────────────────────────────
pydantic_d, pydantic_b, pydantic_h = collect_all("pydantic")

# ── scipy + scikit-image (required by paddleocr via skimage) ──────────────────
scipy_d, scipy_b, scipy_h = collect_all("scipy")
skimage_d, skimage_b, skimage_h = collect_all("skimage")

# ── scipy compiled extensions — force-include .pyd files missed by collect_all ─
import glob as _glob, os as _os
_scipy_base = _os.path.join(
    _os.path.dirname(__import__("scipy").__file__)
)
_scipy_extra_bins = [
    (f, _os.path.join("scipy", _os.path.relpath(_os.path.dirname(f), _scipy_base)))
    for f in _glob.glob(_os.path.join(_scipy_base, "**", "*.pyd"), recursive=True)
]

# ── Cython utility files (.cpp) ───────────────────────────────────────────────
# paddle.utils.cpp_extension imports setuptools which imports Cython at module
# level; Cython then reads its .cpp utility files from disk at import time.
# collect_data_files ensures those files are packaged alongside the .pyc code.
cython_d = collect_data_files("Cython")

all_datas    = paddle_d + paddleocr_d + cv2_d + pydicom_d + pydantic_d + scipy_d + skimage_d + cython_d
all_binaries = paddle_b + paddleocr_b + cv2_b + pydantic_b + scipy_b + skimage_b + _scipy_extra_bins
all_hidden   = (
    paddle_h
    + paddleocr_h
    + cv2_h
    + pydantic_h
    + scipy_h
    + skimage_h
    + collect_submodules("pika")
    + collect_submodules("pydicom")
    + collect_submodules("scipy")
    + collect_submodules("skimage")
    + [
        # pika
        "pika",
        "pika.adapters",
        "pika.adapters.blocking_connection",
        "pika.adapters.utils",
        "pika.compat",
        "pika.credentials",
        # pyodbc
        "pyodbc",
        # dotenv
        "dotenv",
        "python_dotenv",
        # Pillow
        "PIL",
        "PIL.Image",
        "PIL.ImageOps",
        "PIL.ImageFilter",
        # pytesseract
        "pytesseract",
        # numpy
        "numpy",
        # standard libs sometimes missed
        "pkg_resources.py2_warn",
        "logging.handlers",
        # native Windows Service runtime
        "servicemanager",
        "win32event",
        "win32service",
        "win32serviceutil",
        "win32timezone",
    ]
)

a = Analysis(
    ["worker_main.py"],
    pathex=[],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=all_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # GUI / interactive toolkits not needed in a headless worker
        "tkinter",
        "matplotlib",
        "jupyter",
        "IPython",
        "notebook",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DexaOCRWorker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX can break native extensions (paddle, cv2)
    console=True,       # visible terminal in interactive mode; SCM ignores it
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DexaOCRWorker",
)
