"""
Crypto helpers for the e-invoice standard (NIC-style) API transport.

The GSTN e-invoice standard secures POST payloads with:
  - RSA/ECB/PKCS1Padding against the IRP's published public key
    (used for the password + AppKey inside the auth request), and
  - AES-256/ECB/PKCS7Padding with the session key (SEK) returned by auth
    (used for every subsequent request/response payload).

IRIS IRP core APIs follow the same standard. The `cryptography` package backs
both algorithms; import errors surface as a clear HTTPException-friendly
RuntimeError so a missing dependency is diagnosable from the API response.

ECB mode is mandated by the IRP spec — it is not our choice and must not be
"fixed" to CBC/GCM; the IRP cannot decrypt anything else.
"""

import base64
import os


def _require_cryptography():
    try:
        from cryptography.hazmat.primitives import padding as sym_padding  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding  # noqa: F401
        from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "The 'cryptography' package is required for e-invoice NIC-mode transport. "
            "Install it with: pip install cryptography"
        ) from exc


def generate_app_key() -> bytes:
    """Random 32-byte AES key sent (RSA-encrypted) in the auth request."""
    return os.urandom(32)


def rsa_encrypt_b64(plaintext: bytes, public_key_pem: str) -> str:
    """RSA/ECB/PKCS1Padding-encrypt with the IRP public key; return base64."""
    _require_cryptography()
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    public_key = load_pem_public_key(public_key_pem.encode("utf-8"))
    encrypted = public_key.encrypt(plaintext, asym_padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("utf-8")


def aes_encrypt_b64(plaintext: bytes, key: bytes) -> str:
    """AES-256/ECB/PKCS7Padding-encrypt; return base64 (IRP request payloads)."""
    _require_cryptography()
    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("utf-8")


def aes_decrypt(ciphertext_b64: str, key: bytes) -> bytes:
    """Base64-decode then AES-256/ECB/PKCS7Padding-decrypt (IRP response payloads)."""
    _require_cryptography()
    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(base64.b64decode(ciphertext_b64)) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def decrypt_sek(sek_b64: str, app_key: bytes) -> bytes:
    """The auth response's Sek is AES-encrypted with our AppKey; recover it."""
    return aes_decrypt(sek_b64, app_key)


def b64_of(raw: bytes) -> str:
    return base64.b64encode(raw).decode("utf-8")
