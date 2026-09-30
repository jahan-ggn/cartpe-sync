"""AES-GCM request/response encryption for the CartPE API"""

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config.settings import settings

NONCE_BYTES = 12
KEY_HEX_LEN = 64


def get_encryption_key() -> bytes:
    """Derive the 32-byte AES key from the obfuscated key source"""
    key_string = settings.CARTPE_KEY_SOURCE[3:][:-3]
    key_string = key_string[:32] + key_string[36:]
    if len(key_string) != KEY_HEX_LEN:
        raise ValueError(
            f"Derived key must be {KEY_HEX_LEN} hex chars, got {len(key_string)}"
        )
    return bytes.fromhex(key_string)


def encrypt_json(data: dict) -> dict:
    """Encrypt a payload into the API's `{iv, payload}` envelope"""
    iv = os.urandom(NONCE_BYTES)
    plaintext = json.dumps(data, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(get_encryption_key()).encrypt(iv, plaintext, None)
    return {
        "iv": base64.b64encode(iv).decode("utf-8"),
        "payload": base64.b64encode(ciphertext).decode("utf-8"),
    }


def decrypt_json(envelope: dict) -> dict:
    """Decrypt an `{iv, payload}` envelope into a dict"""
    if "iv" not in envelope:
        raise ValueError(f"Response does not contain an IV: {envelope}")
    if "payload" not in envelope:
        raise ValueError(f"Response does not contain a payload: {envelope}")
    iv = base64.b64decode(envelope["iv"])
    ciphertext = base64.b64decode(envelope["payload"])
    plaintext = AESGCM(get_encryption_key()).decrypt(iv, ciphertext, None)
    return json.loads(plaintext.decode("utf-8"))
