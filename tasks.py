"""Developer tasks (invoke). Run ``uv run inv --list``."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from invoke import Context, task

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"


@task
def test(c: Context) -> None:
    """Run the unit tests."""
    c.run("uv run pytest", pty=False)


@task
def run(c: Context, args: str = "--help") -> None:
    """Run the command line tool, e.g. inv run --args='--csv readers.csv --config x.bec --dry-run'."""
    c.run(f"uv run python -m becu.cli {args}", pty=False)


@task
def gui(c: Context) -> None:
    """Start the GUI from the development environment."""
    c.run("uv run python -m becu.gui", pty=False)


def _version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("version not found in pyproject.toml")
    return match.group(1)


def _find_iscc() -> Path | None:
    """The Inno Setup compiler: on the PATH, or in its default installation folders."""
    found = shutil.which("ISCC")
    candidates = [Path(found)] if found else []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates += [Path(base) / "Inno Setup 6" / "ISCC.exe", Path(base) / "Programs" / "Inno Setup 6" / "ISCC.exe"]
    return next((p for p in candidates if p.is_file()), None)


@task
def installer(c: Context) -> None:
    """Build the Windows installer: PyInstaller folder dist/BulkEthernetConfigUploader (GUI + CLI, certificate
    included), then dist/BulkEthernetConfigUploader-<version>-setup.exe with Inno Setup (per-user, no admin)."""
    c.run("uv run pyinstaller --noconfirm --clean --distpath dist --workpath build installer/becu.spec", pty=False)
    bundle = DIST / "BulkEthernetConfigUploader"
    shutil.copytree(ROOT / "certs", bundle / "certs", dirs_exist_ok=True)   # next to the exe, visible and replaceable
    shutil.copyfile(ROOT / "LICENSE", bundle / "LICENSE")
    print(f"program folder: {bundle}")
    iscc = _find_iscc()
    if iscc is None:
        raise SystemExit("Inno Setup not found; install it with: winget install -e --id JRSoftware.InnoSetup")
    version = _version()
    c.run(f'"{iscc}" /Q /DVersion={version} "{ROOT / "installer" / "BulkEthernetConfigUploader.iss"}"', pty=False)
    print(f"installer: {DIST / f'BulkEthernetConfigUploader-{version}-setup.exe'}")
