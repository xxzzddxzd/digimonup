"""PacketEncryptUtil equivalent: RSA-OAEP(SHA1) + AES-256-CBC."""
from __future__ import annotations

import base64
import json
import os
from typing import Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def generate_hex_key() -> str:
    return os.urandom(32).hex()


def generate_hex_iv() -> str:
    return os.urandom(16).hex()


def _from_hex(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


def aes_encrypt(hex_key: str, hex_iv: str, plain_text: str) -> str:
    if not plain_text:
        return ""
    key = _from_hex(hex_key)
    iv = _from_hex(hex_iv)
    data = plain_text.encode("utf-8")
    # PKCS7
    pad_len = 16 - (len(data) % 16)
    data = data + bytes([pad_len] * pad_len)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = encryptor.update(data) + encryptor.finalize()
    return base64.b64encode(ct).decode("ascii")


def aes_decrypt(hex_key: str, hex_iv: str, base64_data: str) -> str:
    if not base64_data:
        return ""
    key = _from_hex(hex_key)
    iv = _from_hex(hex_iv)
    raw = base64.b64decode(base64_data)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    pt = decryptor.update(raw) + decryptor.finalize()
    pad_len = pt[-1]
    if pad_len < 1 or pad_len > 16:
        raise ValueError(f"invalid PKCS7 padding: {pad_len}")
    pt = pt[:-pad_len]
    return pt.decode("utf-8")


def _strip_pem(pem_public_key: str) -> bytes:
    text = pem_public_key
    for token in (
        "-----BEGIN PUBLIC KEY-----",
        "-----END PUBLIC KEY-----",
        "-----BEGIN RSA PUBLIC KEY-----",
        "-----END RSA PUBLIC KEY-----",
    ):
        text = text.replace(token, "")
    text = "".join(text.split())
    return base64.b64decode(text)


def rsa_encrypt(pem_public_key: str, plain_text: str) -> str:
    """RSACryptoServiceProvider.Encrypt(bytes, fOAEP=true) => OAEP-SHA1."""
    key_bytes = _strip_pem(pem_public_key)
    public_key = serialization.load_der_public_key(key_bytes)
    encrypted = public_key.encrypt(
        plain_text.encode("utf-8"),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA1()),
            algorithm=hashes.SHA1(),
            label=None,
        ),
    )
    return base64.b64encode(encrypted).decode("ascii")


def build_encrypted_key(pem_public_key: str, hex_key: str, hex_iv: str) -> str:
    # Unity JsonUtility KeyIvPair field order: key, iv
    payload = json.dumps({"key": hex_key, "iv": hex_iv}, separators=(",", ":"))
    return rsa_encrypt(pem_public_key, payload)


def wrap_encrypted(data_no: str, cipher_b64: str) -> dict:
    return {"_dataNo": data_no, "_data": cipher_b64}


def unwrap_encrypted_response(body: dict, hex_key: str, hex_iv: str) -> dict:
    if not isinstance(body, dict):
        raise TypeError("response body must be dict")
    data = body.get("_data")
    if not data:
        return body
    plain = aes_decrypt(hex_key, hex_iv, data)
    return json.loads(plain)
