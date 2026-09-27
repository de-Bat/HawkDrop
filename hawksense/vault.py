"""Encryption for secrets stored in the database (passwords, tokens, API keys).

Secrets saved in the app are encrypted before they're written, with a key kept
**outside** the database: ``secret.key`` next to the database file (created on
first use, readable only by its owner), or ``HAWKSENSE_SECRET_KEY`` /
``HAWKSENSE_SECRET_KEY_FILE`` (e.g. a Docker secret). A copied database or a
``hawksense backup`` file doesn't reveal them; keep a copy of the key with your
backups if you want the secrets to survive a restore (otherwise just enter
them again).

Standard library only, so no AES: the cipher is HMAC-SHA256 in counter mode
(a PRF-based stream cipher with a random 128-bit nonce) and the ciphertext is
authenticated with a separate HMAC-SHA256 key (encrypt-then-MAC). Both keys are
derived from the master key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

PREFIX = "enc:v1:"
KEY_FILE = "secret.key"


class VaultError(Exception):
    """A secret can't be decrypted (wrong or missing key, or it was tampered with)."""


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def _load_or_create(path: Path) -> bytes:
    try:
        return path.read_bytes().strip()
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    key = base64.b64encode(secrets.token_bytes(32))
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another process created it first
        return path.read_bytes().strip()
    with os.fdopen(fd, "wb") as f:
        f.write(key + b"\n")
    return key


def master_key(db_path: str | Path | None) -> bytes:
    env = os.environ
    if env.get("HAWKSENSE_SECRET_KEY"):
        material = env["HAWKSENSE_SECRET_KEY"].encode()
    elif env.get("HAWKSENSE_SECRET_KEY_FILE"):
        try:
            material = Path(env["HAWKSENSE_SECRET_KEY_FILE"]).read_bytes().strip()
        except OSError as exc:
            raise VaultError(f"can't read HAWKSENSE_SECRET_KEY_FILE: {exc}") from None
    elif db_path is None or str(db_path) == ":memory:":
        material = b"hawksense-ephemeral-" + secrets.token_bytes(16)  # nothing persistent to protect
    else:
        material = _load_or_create(Path(db_path).resolve().parent / KEY_FILE)
    return hashlib.sha256(b"hawksense master key\0" + material).digest()


class Vault:
    def __init__(self, key: bytes):
        self._enc = hmac.new(key, b"encrypt", hashlib.sha256).digest()
        self._mac = hmac.new(key, b"authenticate", hashlib.sha256).digest()

    @classmethod
    def for_db(cls, db) -> "Vault":
        """The vault for a ``hawksense.db.Database`` (key cached on the object)."""
        vault = getattr(db, "_vault", None)
        if vault is None:
            vault = cls(master_key(getattr(db, "path", None)))
            db._vault = vault
        return vault

    def _stream(self, nonce: bytes, length: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < length:
            out += hmac.new(self._enc, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
            counter += 1
        return bytes(out[:length])

    def encrypt(self, plaintext: str) -> str:
        data = plaintext.encode()
        nonce = secrets.token_bytes(16)
        ct = bytes(a ^ b for a, b in zip(data, self._stream(nonce, len(data))))
        tag = hmac.new(self._mac, PREFIX.encode() + nonce + ct, hashlib.sha256).digest()
        return PREFIX + base64.urlsafe_b64encode(nonce + ct + tag).decode()

    def decrypt(self, token: str) -> str:
        if not is_encrypted(token):
            raise VaultError("not an encrypted value")
        try:
            blob = base64.urlsafe_b64decode(token[len(PREFIX):].encode())
        except (ValueError, TypeError):
            raise VaultError("damaged encrypted value") from None
        if len(blob) < 48:
            raise VaultError("damaged encrypted value")
        nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
        expected = hmac.new(self._mac, PREFIX.encode() + nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise VaultError("can't decrypt: it was saved with a different secret key (or changed)")
        return bytes(a ^ b for a, b in zip(ct, self._stream(nonce, len(ct)))).decode()
