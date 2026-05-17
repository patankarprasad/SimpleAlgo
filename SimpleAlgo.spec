from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden_imports = (
    collect_submodules("kiteconnect")
    + collect_submodules("SmartApi")
    + collect_submodules("flask")
    + collect_submodules("jinja2")
    + collect_submodules("werkzeug")
    + collect_submodules("waitress")
    + collect_submodules("apscheduler")
    + [
        "logzero", "pyotp", "pytz", "dotenv",
        "websocket", "websocket._app", "websocket._core",
        "pandas", "numpy",
    ]
)

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["config"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="SimpleAlgo",
    debug=False,
    strip=False,
    upx=True,
    console=True,
    runtime_tmpdir=None,
)
