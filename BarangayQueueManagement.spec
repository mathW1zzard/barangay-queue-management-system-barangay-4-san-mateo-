# -*- mode: python ; coding: utf-8 -*-
# Build with:  pyinstaller --noconfirm --clean BarangayQueueManagement.spec
# Result:      dist\BarangayQueue\BarangayQueueManagement.exe   (the whole dist\BarangayQueue folder is the app)

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# uvicorn, the web-socket library and the form parser are loaded by name at run time,
# so PyInstaller cannot see them by reading the code and they must be listed here.
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("websockets")
    + ["multipart", "python_multipart"]
)

datas = [
    ("templates", "templates"),
    ("static", "static"),          # includes static/fonts and static/audio
    ("app_icon.ico", "."),
]
# python-escpos reads capabilities.json when a printer object is created (even Dummy)
datas += collect_data_files("escpos")

a = Analysis(
    ["run_app.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["alembic", "tkinter"],   # alembic is only for developing the database
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BarangayQueueManagement",
    icon="app_icon.ico",
    console=False,      # no black console window
    upx=False,          # UPX makes antivirus false alarms more likely
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name="BarangayQueueManagement",
)
