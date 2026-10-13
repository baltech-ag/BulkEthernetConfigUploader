"""Test PKI for the mock fleet: a transport-certificate set that mirrors the real one.

Host chain: root -> software CA -> host (the host private key goes into the zip).
Reader chain: root -> production CA; every mock reader gets a certificate signed by the production CA.
The real transport package and its production CA key are never used here.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .crypto import generate_key

NOT_BEFORE = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
NOT_AFTER = dt.datetime(2038, 1, 19, 3, 14, 7, tzinfo=dt.timezone.utc)
ORG = "BECU Test"
ZIP_NAME = "PKI_AUTH_BECU_TEST.zip"


def _name(common_name: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "DE"),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG),
    ])


def make_cert(subject: x509.Name, public_key: ec.EllipticCurvePublicKey, issuer: x509.Name,
              issuer_key: ec.EllipticCurvePrivateKey, serial: int, ca: bool) -> x509.Certificate:
    builder = (x509.CertificateBuilder()
               .subject_name(subject)
               .issuer_name(issuer)
               .public_key(public_key)
               .serial_number(serial)
               .not_valid_before(NOT_BEFORE)
               .not_valid_after(NOT_AFTER))
    if ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    return builder.sign(issuer_key, hashes.SHA256())


@dataclass
class TestPki:
    root_key: ec.EllipticCurvePrivateKey
    root_cert: x509.Certificate
    software_ca_key: ec.EllipticCurvePrivateKey
    software_ca_cert: x509.Certificate
    host_key: ec.EllipticCurvePrivateKey
    host_cert: x509.Certificate
    production_ca_key: ec.EllipticCurvePrivateKey
    production_ca_cert: x509.Certificate

    @classmethod
    def generate(cls) -> "TestPki":
        root_key = generate_key()
        root_name = _name("BECU Test Transport Root Certificate")
        root_cert = make_cert(root_name, root_key.public_key(), root_name, root_key, 1, ca=True)
        software_ca_key = generate_key()
        software_ca_cert = make_cert(_name("BECU Test Software CA Certificate"), software_ca_key.public_key(),
                                     root_name, root_key, 2, ca=True)
        host_key = generate_key()
        host_cert = make_cert(_name("BECU Test Host Certificate"), host_key.public_key(),
                              software_ca_cert.subject, software_ca_key, 3, ca=False)
        production_ca_key = generate_key()
        production_ca_cert = make_cert(_name("BECU Test Production CA Certificate"), production_ca_key.public_key(),
                                       root_name, root_key, 4, ca=True)
        return cls(root_key, root_cert, software_ca_key, software_ca_cert, host_key, host_cert,
                   production_ca_key, production_ca_cert)

    def make_reader_cert(self, serial: str, key: ec.EllipticCurvePrivateKey) -> x509.Certificate:
        return make_cert(_name(f"S# {serial}"), key.public_key(), self.production_ca_cert.subject,
                         self.production_ca_key, 1000 + int(serial), ca=False)

    # ---- files -------------------------------------------------------------------------------------

    def write_zip(self, path: Path, security_level: int = 1) -> Path:
        """The host-side package in the PkiAuthZip layout, usable with ``--cert``."""
        pem = lambda cert: cert.public_bytes(serialization.Encoding.PEM)  # noqa: E731
        key_pem = self.host_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                              serialization.NoEncryption())
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("host_chain/0root.crt", pem(self.root_cert))
            z.writestr("host_chain/1software_ca.crt", pem(self.software_ca_cert))
            z.writestr("host_chain/2host.crt", pem(self.host_cert))
            z.writestr("reader_chain/0root.crt", pem(self.root_cert))
            z.writestr("reader_chain/1production_ca.crt", pem(self.production_ca_cert))
            z.writestr("keypair.key", key_pem)
            z.writestr("settings.cfg", f"[settings]\nsecurity_level = {security_level}\n")
            z.writestr("README.txt", "Test transport keys of the BECU mock fleet. Not valid for real readers.\n")
        return path

    def save(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        for name, key in (("root", self.root_key), ("software_ca", self.software_ca_key),
                          ("host", self.host_key), ("production_ca", self.production_ca_key)):
            (folder / f"{name}.key").write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        for name, cert in (("root", self.root_cert), ("software_ca", self.software_ca_cert),
                           ("host", self.host_cert), ("production_ca", self.production_ca_cert)):
            (folder / f"{name}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.write_zip(folder / ZIP_NAME)

    @classmethod
    def load(cls, folder: Path) -> "TestPki":
        def key(name: str) -> ec.EllipticCurvePrivateKey:
            loaded = serialization.load_pem_private_key((folder / f"{name}.key").read_bytes(), password=None)
            assert isinstance(loaded, ec.EllipticCurvePrivateKey)
            return loaded

        def cert(name: str) -> x509.Certificate:
            return x509.load_pem_x509_certificate((folder / f"{name}.crt").read_bytes())

        return cls(key("root"), cert("root"), key("software_ca"), cert("software_ca"), key("host"), cert("host"),
                   key("production_ca"), cert("production_ca"))

    @classmethod
    def load_or_generate(cls, folder: Path) -> "TestPki":
        if (folder / "root.key").is_file() and (folder / ZIP_NAME).is_file():
            return cls.load(folder)
        pki = cls.generate()
        pki.save(folder)
        return pki
