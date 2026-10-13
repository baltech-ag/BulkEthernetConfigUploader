# PyInstaller spec: one folder with the GUI (BulkEthernetConfigUploader.exe) and the CLI (becu-cli.exe).
# Build with "uv run inv installer" (or: uv run pyinstaller installer/becu.spec).
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent                                   # SPECPATH is this installer/ folder
sdk_datas, sdk_binaries, sdk_hidden = collect_all("baltech_sdk")   # the brp_lib DLLs are package data
# certs/ and LICENSE are copied next to the exe by the installer task, not into _internal/
common = dict(pathex=[str(root)], binaries=sdk_binaries, datas=sdk_datas, hiddenimports=sdk_hidden,
              excludes=["tkinter", "pytest", "invoke", "cryptography", "mock", "tests"])

gui_a = Analysis([str(root / "installer" / "gui_entry.py")], **common)
cli_a = Analysis([str(root / "installer" / "cli_entry.py")], **common)
gui_exe = EXE(PYZ(gui_a.pure), gui_a.scripts, [], exclude_binaries=True, name="BulkEthernetConfigUploader",
              console=False)
cli_exe = EXE(PYZ(cli_a.pure), cli_a.scripts, [], exclude_binaries=True, name="becu-cli", console=True)
COLLECT(gui_exe, gui_a.binaries, gui_a.datas, cli_exe, cli_a.binaries, cli_a.datas,
        name="BulkEthernetConfigUploader")
