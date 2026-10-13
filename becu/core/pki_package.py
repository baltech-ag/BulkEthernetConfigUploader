"""PKI transport certificate package (PkiAuthZip layout).

Layout, as consumed by the SDK's PKI layer: ``host_chain/<digit>…crt`` (root … host, at least
two), ``reader_chain/<digit>…crt`` (at least one), ``keypair.key`` (host private key),
``settings.cfg`` with ``[settings] security_level = N``, optional ``README.txt``.
"""

from __future__ import annotations

import configparser
import zipfile
from dataclasses import dataclass
import sys
from pathlib import Path

from .errors import PkiPackageError


@dataclass(frozen=True)
class PkiPackage:
    path: Path
    security_level: int
    host_chain: tuple[str, ...]      #: file names, sorted as the SDK sorts them
    reader_chain: tuple[str, ...]
    readme: str

    def describe(self) -> str:
        return (f"{self.path.name}: security level {self.security_level}, "
                f"host chain {len(self.host_chain)} certificates, "
                f"reader chain {len(self.reader_chain)} certificates")


def project_root() -> Path:
    """Folder with certs/, logs/ and reports/: the checkout, or the installation folder of the frozen program."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_certs_dir() -> Path:
    return project_root() / "certs"


def find_default_package(certs_dir: Path | None = None) -> Path:
    """The single ``*.zip`` in the certs folder. Raises when there is none or more than one."""
    folder = certs_dir or default_certs_dir()
    zips = sorted(folder.glob("*.zip")) if folder.is_dir() else []
    if not zips:
        raise PkiPackageError(f"no certificate package (*.zip) found in {folder}; "
                              f"see {folder / 'README.md'} or pass --cert")
    if len(zips) > 1:
        names = ", ".join(z.name for z in zips)
        raise PkiPackageError(f"more than one *.zip in {folder} ({names}); pass --cert to choose one")
    return zips[0]


def load_pki_package(path: str | Path) -> PkiPackage:
    path = Path(path)
    if not path.is_file():
        raise PkiPackageError(f"certificate package not found: {path}")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise PkiPackageError(f"{path.name}: not a zip file") from None
    with archive:
        names = archive.namelist()
        host_chain = sorted(n for n in names if n.startswith("host_chain/") and n.endswith(".crt"))
        reader_chain = sorted(n for n in names if n.startswith("reader_chain/") and n.endswith(".crt"))
        if len(host_chain) < 2:
            raise PkiPackageError(f"{path.name}: host_chain/ needs at least two .crt files (root … host)")
        if len(reader_chain) < 1:
            raise PkiPackageError(f"{path.name}: reader_chain/ needs at least one .crt file")
        for name in host_chain + reader_chain:
            if not name.rsplit("/", 1)[-1][:1].isdigit():
                raise PkiPackageError(f"{path.name}: certificate {name} must start with a digit (chain order)")
        if "keypair.key" not in names:
            raise PkiPackageError(f"{path.name}: keypair.key is missing")
        if "settings.cfg" not in names:
            raise PkiPackageError(f"{path.name}: settings.cfg is missing")
        parser = configparser.ConfigParser()
        try:
            parser.read_string(archive.read("settings.cfg").decode("utf-8"))
            security_level = parser.getint("settings", "security_level")
        except (configparser.Error, UnicodeDecodeError, ValueError) as e:
            raise PkiPackageError(f"{path.name}: settings.cfg is invalid: {e}") from None
        if not 1 <= security_level <= 3:
            raise PkiPackageError(f"{path.name}: security_level must be 1..3, found {security_level}")
        readme = ""
        for candidate in ("README.txt", "readme.txt"):
            if candidate in names:
                readme = archive.read(candidate).decode("utf-8", errors="replace")
                break
    return PkiPackage(path, security_level, tuple(host_chain), tuple(reader_chain), readme)
