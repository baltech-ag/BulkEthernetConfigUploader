"""Crypto primitives of the PKI secure channel (reader side), on top of ``cryptography``.

Matches the host side of the protocol in brp_lib (pki_protocol.c):
AES-128-CBC with zero padding, CBC-MAC as "HMAC", ECDH on P-256 (x coordinate), SHA-256.
"""

from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_der_public_key

BLOCK = 16


def pad_zero(data: bytes) -> bytes:
    rest = len(data) % BLOCK
    return data + b"\x00" * (BLOCK - rest) if rest else data


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Encrypt ``data`` (zero-padded to 16 bytes) with AES-128-CBC."""
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(pad_zero(data)) + encryptor.finalize()


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % BLOCK:
        raise ValueError("ciphertext length is not a multiple of 16")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def cbc_mac(key: bytes, iv: bytes, data: bytes) -> bytes:
    """The protocol's "HMAC": last AES-CBC block over the zero-padded data."""
    return aes_cbc_encrypt(key, iv, data)[-BLOCK:]


def auth_cipher_params(shared_secret_1: bytes) -> tuple[bytes, bytes]:
    """(key, iv) of the handshake cipher derived from the ECDH secret of the temporary keys."""
    digest = hashlib.sha256(shared_secret_1).digest()
    return digest[0:16], digest[16:32]


def seq_iv(unique: int, seq_counter: int) -> bytes:
    return bytes([unique]) + seq_counter.to_bytes(15, "big")


def ecdh(private_key: ec.EllipticCurvePrivateKey, peer_public_key: ec.EllipticCurvePublicKey) -> bytes:
    """32-byte shared secret (x coordinate), as uECC computes it."""
    return private_key.exchange(ec.ECDH(), peer_public_key)


def generate_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def public_key_der(key: ec.EllipticCurvePrivateKey | ec.EllipticCurvePublicKey) -> bytes:
    public = key.public_key() if isinstance(key, ec.EllipticCurvePrivateKey) else key
    return public.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)


def load_public_key_der(data: bytes) -> ec.EllipticCurvePublicKey:
    key = load_der_public_key(data)
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ValueError("not an EC public key")
    return key


def verify_signature(issuer_public_key: ec.EllipticCurvePublicKey, cert) -> None:
    """Raise ``cryptography.exceptions.InvalidSignature`` when ``cert`` was not signed by the issuer key."""
    issuer_public_key.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256()))
