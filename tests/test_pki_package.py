import zipfile
from pathlib import Path

import pytest

from becu.core.errors import PkiPackageError
from becu.core.pki_package import find_default_package, load_pki_package

PEM = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
KEY = "-----BEGIN EC PRIVATE KEY-----\nAAAA\n-----END EC PRIVATE KEY-----\n"


def make_zip(path: Path, *, host=("0root.crt", "1ca.crt", "2host.crt"), reader=("0root.crt", "1prod.crt"),
             key=True, settings="[settings]\nsecurity_level = 1\n", readme="transport keys") -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name in host:
            z.writestr(f"host_chain/{name}", PEM)
        for name in reader:
            z.writestr(f"reader_chain/{name}", PEM)
        if key:
            z.writestr("keypair.key", KEY)
        if settings is not None:
            z.writestr("settings.cfg", settings)
        if readme:
            z.writestr("README.txt", readme)
    return path


def test_load_valid_package(tmp_path):
    pkg = load_pki_package(make_zip(tmp_path / "p.zip"))
    assert pkg.security_level == 1
    assert pkg.host_chain == ("host_chain/0root.crt", "host_chain/1ca.crt", "host_chain/2host.crt")
    assert pkg.reader_chain == ("reader_chain/0root.crt", "reader_chain/1prod.crt")
    assert "transport" in pkg.readme
    assert "security level 1" in pkg.describe()


@pytest.mark.parametrize("kwargs, message", [
    (dict(host=("2host.crt",)), "host_chain"),
    (dict(reader=()), "reader_chain"),
    (dict(host=("0root.crt", "host.crt")), "digit"),
    (dict(key=False), "keypair.key"),
    (dict(settings=None), "settings.cfg"),
    (dict(settings="[settings]\nsecurity_level = 7\n"), "1..3"),
    (dict(settings="nonsense"), "invalid"),
])
def test_invalid_packages(tmp_path, kwargs, message):
    with pytest.raises(PkiPackageError, match=message):
        load_pki_package(make_zip(tmp_path / "p.zip", **kwargs))


def test_not_a_zip_and_missing(tmp_path):
    bad = tmp_path / "x.zip"
    bad.write_text("hello")
    with pytest.raises(PkiPackageError, match="not a zip"):
        load_pki_package(bad)
    with pytest.raises(PkiPackageError, match="not found"):
        load_pki_package(tmp_path / "none.zip")


def test_find_default_package(tmp_path):
    with pytest.raises(PkiPackageError, match="no certificate package"):
        find_default_package(tmp_path)
    one = make_zip(tmp_path / "a.zip")
    assert find_default_package(tmp_path) == one
    make_zip(tmp_path / "b.zip")
    with pytest.raises(PkiPackageError, match="more than one"):
        find_default_package(tmp_path)
