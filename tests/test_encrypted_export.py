import json
from pathlib import Path
import shutil
import tarfile

import pytest

from lenkobot.encrypted_export import SQLiteEncryptedExporter
from lenkobot.sqlite_schema import open_state_database


class CopyEncryptor:
    def __init__(self):
        self.recipient = None

    def encrypt(self, source: Path, destination: Path, recipient: str) -> None:
        self.recipient = recipient
        shutil.copyfile(source, destination)


def test_export_is_a_pax_archive_with_consistent_snapshot_and_manifest(tmp_path):
    database_path = tmp_path / "state.db"
    connection = open_state_database(database_path)
    with connection:
        connection.execute(
            "INSERT INTO user_profile (user_id, created_at) VALUES (42, 'now')"
        )
    connection.close()
    encryptor = CopyEncryptor()

    destination = SQLiteEncryptedExporter(
        database_path,
        encryptor=encryptor,
        temp_root=tmp_path,
    ).export(tmp_path / "backup.age", recipient="age1example")

    assert destination.is_file()
    assert encryptor.recipient == "age1example"
    with tarfile.open(destination, mode="r") as archive:
        assert archive.format == tarfile.PAX_FORMAT
        assert archive.getnames() == ["manifest.json", "state.db"]
        manifest = json.load(archive.extractfile("manifest.json"))
        assert manifest["format"] == "lenkobot-state-export-v1"
        assert manifest["files"] == ["state.db"]
        snapshot = archive.extractfile("state.db")
        assert snapshot is not None
        snapshot.read(16)


@pytest.mark.parametrize("recipient", ("", "x25519", "age1 with spaces"))
def test_export_rejects_invalid_recipient(tmp_path, recipient):
    database_path = tmp_path / "state.db"
    open_state_database(database_path).close()

    with pytest.raises(ValueError, match="recipient"):
        SQLiteEncryptedExporter(database_path).export(
            tmp_path / "backup.age",
            recipient=recipient,
        )
