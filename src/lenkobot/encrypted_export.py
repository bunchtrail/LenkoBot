from datetime import datetime, timezone
import json
from pathlib import Path
import os
import sqlite3
import subprocess
import tarfile
import tempfile
from typing import Protocol

from .sqlite_schema import CURRENT_SCHEMA_VERSION


class ExportError(RuntimeError):
    pass


class AgeEncryptor(Protocol):
    def encrypt(self, source: Path, destination: Path, recipient: str) -> None: ...


class SubprocessAgeEncryptor:
    def encrypt(self, source: Path, destination: Path, recipient: str) -> None:
        try:
            subprocess.run(
                [
                    "age",
                    "--encrypt",
                    "--recipient",
                    recipient,
                    "--output",
                    str(destination),
                    str(source),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise ExportError("encrypted export failed") from error


class SQLiteEncryptedExporter:
    def __init__(
        self,
        database_path: Path | str,
        *,
        encryptor: AgeEncryptor | None = None,
        temp_root: Path | str | None = None,
    ) -> None:
        self._database_path = Path(database_path)
        self._encryptor = encryptor or SubprocessAgeEncryptor()
        self._temp_root = None if temp_root is None else Path(temp_root)

    def export(
        self,
        destination: Path | str,
        *,
        recipient: str,
    ) -> Path:
        _validate_recipient(recipient)
        destination = Path(destination)
        if destination.suffix != ".age":
            raise ValueError("encrypted export destination must end with .age")
        if not self._database_path.is_file():
            raise ExportError("state database is unavailable")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="lenkobot-export-",
            dir=None if self._temp_root is None else str(self._temp_root),
        ) as temporary:
            workspace = Path(temporary)
            snapshot = workspace / "state.db"
            archive = workspace / "export.tar"
            encrypted = workspace / "export.age"
            self._snapshot(snapshot)
            manifest = {
                "format": "lenkobot-state-export-v1",
                "schema_version": CURRENT_SCHEMA_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "files": ["state.db"],
            }
            manifest_path = workspace / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=True, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with tarfile.open(archive, mode="w", format=tarfile.PAX_FORMAT) as tar:
                tar.add(manifest_path, arcname="manifest.json", recursive=False)
                tar.add(snapshot, arcname="state.db", recursive=False)
            self._encryptor.encrypt(archive, encrypted, recipient)
            if not encrypted.is_file():
                raise ExportError("encrypted export produced no archive")
            os.replace(encrypted, destination)
        return destination

    def _snapshot(self, destination: Path) -> None:
        source = sqlite3.connect(self._database_path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            target.commit()
        except sqlite3.Error as error:
            raise ExportError("state database snapshot failed") from error
        finally:
            target.close()
            source.close()


def _validate_recipient(recipient: str) -> None:
    if (
        not isinstance(recipient, str)
        or not recipient.startswith("age1")
        or len(recipient) < 10
        or len(recipient) > 200
        or any(character.isspace() for character in recipient)
    ):
        raise ValueError("age recipient is invalid")
