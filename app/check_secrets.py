import base64
import hashlib
import json
import re

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def _fernet(credential_key=None):
    if credential_key is None:
        credential_key = current_app.config.get("CHECK_CREDENTIAL_KEY", "")
    if not isinstance(credential_key, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", credential_key):
        raise ValueError(
            "CHECK_CREDENTIAL_KEY must be configured as 64 hexadecimal characters."
        )
    return Fernet(base64.urlsafe_b64encode(bytes.fromhex(credential_key)))


def encrypt_credentials(username, password):
    payload = json.dumps(
        {"username": username, "password": password},
        separators=(",", ":"),
    ).encode("utf-8")
    return _fernet().encrypt(payload).decode("ascii")


def decrypt_credentials(token, credential_key=None, secret_key=None):
    try:
        payload = _fernet(credential_key=credential_key).decrypt(token.encode("ascii"))
    except InvalidToken:
        payload = _decrypt_legacy_token(token, secret_key=secret_key)
    except (UnicodeDecodeError, TypeError, ValueError) as error:
        raise ValueError(
            "Stored credentials cannot be decrypted. Check the credential-encryption and platform keys."
        ) from error
    try:
        credentials = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise ValueError("Stored check credentials are malformed.") from error
    username = credentials.get("username")
    password = credentials.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError("Stored check credentials are incomplete.")
    return username, password


def _decrypt_legacy_token(token, secret_key=None):
    if secret_key is None:
        secret_key = current_app.config["SECRET_KEY"]
    if isinstance(secret_key, str):
        secret_key = secret_key.encode("utf-8")
    legacy_key = hashlib.sha256(b"competition-check-credentials\0" + secret_key).digest()
    legacy_fernet = Fernet(base64.urlsafe_b64encode(legacy_key))
    try:
        return legacy_fernet.decrypt(token.encode("ascii"))
    except (InvalidToken, UnicodeDecodeError, TypeError) as error:
        raise ValueError(
            "Stored credentials cannot be decrypted. Check the credential-encryption and platform keys."
        ) from error
