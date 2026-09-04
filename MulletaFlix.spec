# -*- coding: utf-8 -*-
"""
PyInstaller spec for Mulletaflix.
Builds a single executable with all dependencies and embedded configuration.
"""

import os
import sys
from pathlib import Path

# Project root - spec file is in the project root
SRC_DIR = Path.cwd()
PROJECT_ROOT = SRC_DIR

# Determine if we're building for mulletaflix or original
BUILD_VARIANT = os.getenv("BUILD_VARIANT", "mulletaflix")  # "mulletaflix" or "original"

if BUILD_VARIANT == "mulletaflix":
    APP_NAME = "MulletaFlix"
    ENTRY_POINT = SRC_DIR / "main.py"
    CONFIG_SOURCE = SRC_DIR / ".env.mulletaflix"
    ICON_FILE = SRC_DIR / "img" / "mulletaflix.ico" if (SRC_DIR / "img" / "mulletaflix.ico").exists() else None
else:
    APP_NAME = "NebulaFTP"
    ENTRY_POINT = SRC_DIR / "main.py"
    CONFIG_SOURCE = SRC_DIR / ".env"
    ICON_FILE = SRC_DIR / "img" / "nebula.ico" if (SRC_DIR / "img" / "nebula.ico").exists() else None

# Hidden imports for PyInstaller
HIDDEN_IMPORTS = [
    # Core
    "motor.motor_asyncio",
    "pymongo",
    "bson",
    "pyrogram",
    "pyrogram.handlers",
    "pyrogram.types",
    "pyrogram.filters",
    "pyrogram.enums",
    "pyrogram.errors",
    "pyrogram.client",
    "pyrogram.methods",
    "pyrogram.methods.messages",
    "pyrogram.methods.chats",
    "pyrogram.methods.users",
    "aiohttp",
    "aiofiles",
    "dotenv",
    "cryptography",
    "bcrypt",
    "dnspython",
    "tgcrypto",
    "pytz",
    "dateutil",
    "dateutil.parser",
    "dateutil.tz",
    # Supabase
    "supabase",
    "supabase.client",
    "supabase.lib.client_options",
    "gotrue",
    "gotrue.types",
    "gotrue.errors",
    "postgrest",
    "postgrest.types",
    "postgrest.exceptions",
    "realtime",
    "realtime.client",
    "realtime.types",
    # Stdlib modules that PyInstaller sometimes misses
    "asyncio",
    "json",
    "logging",
    "pathlib",
    "urllib.parse",
    "html",
    "signal",
    "ssl",
    "subprocess",
    "threading",
    "multiprocessing",
    "collections",
    "itertools",
    "functools",
    "dataclasses",
    "typing",
    "uuid",
    "hashlib",
    "hmac",
    "base64",
    "mimetypes",
    "email.utils",
    "email.message",
    "ipaddress",
    "re",
    "time",
    "datetime",
    "decimal",
    "fractions",
    "numbers",
    "string",
    "textwrap",
    "unicodedata",
    "zoneinfo",
]

# Exclude unnecessary modules to reduce size
EXCLUDES = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "PIL.ImageShow",
    "PIL.ImageQt",
    "test",
    "unittest",
    "pytest",
    "setuptools",
    "pip",
    "wheel",
    "pkg_resources",
    "distutils",
    "ctypes",
    "curses",
    "dbm",
    "sqlite3",
    "readline",
    "rlcompleter",
    "turtle",
    "xmlrpc",
    "http.server",
    "socketserver",
    "xml.etree",
    "xml.dom",
    "xml.sax",
    "html.parser",
    "html.entities",
    "email.mime",
    "email.header",
    "email.charset",
    "email.encoders",
    "email.utils",
    "email.message",
    "email.parser",
    "email.generator",
    "email.iterators",
    "email.policy",
    "email.errors",
    "email.base64mime",
    "email.quoprimime",
    "email.feedparser",
    "email.headerregistry",
    "email.contentmanager",
]

# Data files to include
DATAS = [
    # Embed the configuration file as .env
    (str(CONFIG_SOURCE), "."),
    # Include any additional config files
    (str(SRC_DIR / "requirements.txt"), "."),
    (str(SRC_DIR / "requirements.in"), "."),
    (str(SRC_DIR / "pyproject.toml"), "."),
    # Include img directory if exists
    (str(SRC_DIR / "img"), "img") if (SRC_DIR / "img").exists() else None,
    # Include ftp module
    (str(SRC_DIR / "ftp"), "ftp"),
    # Include tools that are needed at runtime
    (str(SRC_DIR / "tools" / "supabase_sync.py"), "tools"),
    (str(SRC_DIR / "tools" / "bootstrap.py"), "tools"),
    (str(SRC_DIR / "tools" / "clean_already_sent.py"), "tools"),
    (str(SRC_DIR / "tools" / "feed_ftp.py"), "tools"),
    (str(SRC_DIR / "tools" / "start_rclone_z.ps1"), "tools"),
]

# Filter out None entries
DATAS = [d for d in DATAS if d is not None]

# Binary dependencies (DLLs, etc.)
BINARIES = []

# Runtime hooks
RUNTIME_HOOKS = []

# Build options
BUILD_OPTIONS = {
    "name": APP_NAME,
    "version": "1.0.0",
    "description": "MulletaFlix - Media streaming server with Telegram integration",
    "author": "MulletaFlix Team",
    "icon": str(ICON_FILE) if ICON_FILE else None,
    "console": True,  # Set to False for GUI-only
    "onefile": True,
    "clean": True,
    "noconfirm": True,
    "strip": True,
    "upx": True,
    "upx_exclude": ["vcruntime140.dll", "python314.dll"],
    "hiddenimports": HIDDEN_IMPORTS,
    "excludes": EXCLUDES,
    "datas": DATAS,
    "binaries": BINARIES,
    "runtime_hooks": RUNTIME_HOOKS,
    "workpath": str(PROJECT_ROOT / "build" / "work"),
    "distpath": str(PROJECT_ROOT / "dist"),
    "specpath": str(PROJECT_ROOT / "build"),
}

# Create the spec
from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT

a = Analysis(
    [str(ENTRY_POINT)],
    pathex=[str(SRC_DIR)],
    binaries=BINARIES,
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=RUNTIME_HOOKS,
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=BUILD_OPTIONS["strip"],
    upx=BUILD_OPTIONS["upx"],
    upx_exclude=BUILD_OPTIONS["upx_exclude"],
    runtime_tmpdir=None,
    console=BUILD_OPTIONS["console"],
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=BUILD_OPTIONS["icon"],
    version_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=BUILD_OPTIONS["strip"],
    upx=BUILD_OPTIONS["upx"],
    upx_exclude=BUILD_OPTIONS["upx_exclude"],
    name=APP_NAME,
)