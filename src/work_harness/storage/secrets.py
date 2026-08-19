from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class ProviderSecretCipher:
    """Authenticated encryption for UI-managed Provider credentials."""

    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path
        self._fernet: Fernet | None = None

    def initialize(self) -> None:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            key = self.key_path.read_bytes()
        except FileNotFoundError:
            key = Fernet.generate_key()
            try:
                descriptor = os.open(
                    self.key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                key = self.key_path.read_bytes()
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(key)
        if os.name != "nt":
            os.chmod(self.key_path, 0o600)
        try:
            self._fernet = Fernet(key.strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid Provider secret key: {self.key_path}") from exc

    def encrypt(self, value: str) -> bytes:
        if self._fernet is None:
            raise RuntimeError("Provider secret cipher is not initialized")
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, value: bytes) -> str:
        if self._fernet is None:
            raise RuntimeError("Provider secret cipher is not initialized")
        try:
            return self._fernet.decrypt(value).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise ValueError(
                "stored Provider credential cannot be decrypted with the current key"
            ) from exc
