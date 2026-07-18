import pytest

from lenkobot.memory import MemoryCategory, MemoryScope, MemorySource, NewMemory, SQLiteMemoryStore
from lenkobot.session_deletion import SQLiteSessionDataDeletionService
from lenkobot.sqlite_schema import open_state_database


def active_exchange(database_path):
    connection = open_state_database(database_path)
    with connection:
        connection.execute(
            "INSERT INTO user_profile (user_id, created_at) VALUES (42, 'now')"
        )
        connection.execute(
            """
            INSERT INTO conversation (id, platform, chat_id, active_persona_key)
            VALUES (1, 'telegram', 500, 'companion')
            """
        )
        connection.execute(
            """
            INSERT INTO persona_session (id, conversation_id, persona_key, identity_version)
            VALUES (1, 1, 'companion', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO session (
                id, persona_session_id, owner_user_id, generation, status, opened_at
            ) VALUES (1, 1, 42, 1, 'active', 'now')
            """
        )
        connection.execute(
            """
            INSERT INTO transcript_turn (
                id, session_id, sequence, role, content, created_at
            ) VALUES (1, 1, 1, 'user', 'remember me', 'now')
            """
        )
    connection.close()


def test_delete_turn_cascades_automatic_memory_revisions_and_invalidates_summary(tmp_path):
    database_path = tmp_path / "state.db"
    active_exchange(database_path)
    memory_store = SQLiteMemoryStore(database_path)
    record = memory_store.create(
        NewMemory(
            user_id=42,
            scope=MemoryScope.SHARED,
            kind="preference",
            content="Concise answers",
            source=MemorySource.AUTOMATIC,
            category=MemoryCategory.PREFERENCE,
            confidence=0.8,
            provenance_session_id=1,
            provenance_turn_id=1,
        )
    )
    memory_store.update(record.id, user_id=42, content="Very concise answers")
    connection = open_state_database(database_path)
    with connection:
        connection.execute(
            """
            INSERT INTO session_summary (
                session_id, owner_user_id, content, source_turn_count,
                lifecycle_epoch, status, created_at
            ) VALUES (1, 42, 'Summary', 1, 1, 'active', 'now')
            """
        )
    connection.close()

    result = SQLiteSessionDataDeletionService(database_path).delete_turn(
        owner_user_id=42,
        session_id=1,
        turn_id=1,
    )

    assert result.deleted_memory_count == 1
    assert result.invalidated_summary is True
    assert memory_store.memory_count() == 0
    assert memory_store.list_revisions(record.id, user_id=42) == ()
    check = open_state_database(database_path)
    assert check.execute("SELECT COUNT(*) FROM transcript_turn").fetchone()[0] == 0
    assert check.execute("SELECT status FROM session_summary").fetchone()[0] == "invalidated"
    check.close()


def test_delete_turn_enforces_owner_acl(tmp_path):
    database_path = tmp_path / "state.db"
    active_exchange(database_path)
    service = SQLiteSessionDataDeletionService(database_path)

    with pytest.raises(KeyError):
        service.delete_turn(owner_user_id=99, session_id=1, turn_id=1)

    connection = open_state_database(database_path)
    assert connection.execute("SELECT COUNT(*) FROM transcript_turn").fetchone()[0] == 1
    connection.close()
