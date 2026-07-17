import sqlite3

import pytest

from lenkobot.memory import MemoryScope, NewMemory, SQLiteMemoryStore
from lenkobot.personas import Persona, PersonaCatalog
from lenkobot.sqlite_schema import (
    CURRENT_SCHEMA_VERSION,
    SchemaVersionError,
    open_state_database,
)
from lenkobot.telegram_router import IncomingTelegramMessage, SQLiteConversationStore


def catalog():
    return PersonaCatalog(
        (
            Persona(
                key="companion",
                display_name="Companion",
                identity_prompt="A calm companion.",
                identity_version=1,
            ),
        ),
        default_persona_key="companion",
    )


def test_unversioned_state_database_is_migrated_without_losing_session_data(tmp_path):
    database_path = tmp_path / "state.db"
    legacy = sqlite3.connect(database_path)
    legacy.executescript(
        """
        CREATE TABLE conversation (
            id INTEGER PRIMARY KEY,
            platform TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            active_persona_key TEXT NOT NULL,
            UNIQUE(platform, chat_id)
        );
        CREATE TABLE persona_session (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversation(id),
            persona_key TEXT NOT NULL,
            identity_version INTEGER NOT NULL,
            UNIQUE(conversation_id, persona_key, identity_version)
        );
        INSERT INTO conversation (id, platform, chat_id, active_persona_key)
        VALUES (7, 'telegram', 500, 'companion');
        INSERT INTO persona_session (
            id, conversation_id, persona_key, identity_version
        ) VALUES (11, 7, 'companion', 1);
        """
    )
    legacy.close()

    conversation_store = SQLiteConversationStore(database_path)
    turn = conversation_store.route_message(
        IncomingTelegramMessage(
            user_id=42,
            chat_id=500,
            chat_type="private",
            text="hello",
        ),
        catalog(),
    )
    conversation_store.close()
    memory_store = SQLiteMemoryStore(database_path)
    persona_id = memory_store.register_persona(catalog().get("companion"))
    record = memory_store.create(
        NewMemory(
            user_id=42,
            scope=MemoryScope.SHARED,
            kind="fact",
            content="kept",
        )
    )
    memory_store.close()

    connection = open_state_database(database_path)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    conversation = connection.execute(
        "SELECT id, active_persona_key, version FROM conversation WHERE chat_id = 500"
    ).fetchone()
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    legacy_lane = connection.execute(
        "SELECT id, conversation_id FROM persona_session WHERE id = 11"
    ).fetchone()
    connection.close()

    assert turn.conversation_id == 7
    assert turn.session_id == 11
    assert persona_id > 0
    assert record.content == "kept"
    assert version == CURRENT_SCHEMA_VERSION
    assert tuple(conversation) == (7, "companion", 1)
    assert tuple(legacy_lane) == (11, 7)
    assert {
        "user_profile",
        "session",
        "transcript_turn",
        "transcript_failure",
    } <= tables


def test_newer_schema_version_is_rejected_without_modifying_database(tmp_path):
    database_path = tmp_path / "state.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    connection.execute("INSERT INTO marker (value) VALUES ('unchanged')")
    connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()

    with pytest.raises(SchemaVersionError):
        open_state_database(database_path)

    check = sqlite3.connect(database_path)
    assert check.execute("SELECT value FROM marker").fetchone()[0] == "unchanged"
    assert check.execute("PRAGMA user_version").fetchone()[0] == (
        CURRENT_SCHEMA_VERSION + 1
    )
    check.close()


def test_version_three_database_adds_session_schema_without_rewriting_lane_ids(tmp_path):
    database_path = tmp_path / "state.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE conversation (
            id INTEGER PRIMARY KEY,
            platform TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            active_persona_key TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0,
            UNIQUE(platform, chat_id)
        );
        CREATE TABLE persona_session (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversation(id),
            persona_key TEXT NOT NULL,
            identity_version INTEGER NOT NULL,
            UNIQUE(conversation_id, persona_key, identity_version)
        );
        INSERT INTO conversation (
            id, platform, chat_id, active_persona_key, version
        ) VALUES (7, 'telegram', 500, 'companion', 3);
        INSERT INTO persona_session (
            id, conversation_id, persona_key, identity_version
        ) VALUES (11, 7, 'companion', 1);
        PRAGMA user_version = 3;
        """
    )
    connection.close()

    migrated = open_state_database(database_path)

    assert migrated.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert tuple(
        migrated.execute(
            "SELECT id, conversation_id FROM persona_session WHERE id = 11"
        ).fetchone()
    ) == (11, 7)
    assert migrated.execute(
        "SELECT COUNT(*) FROM session"
    ).fetchone()[0] == 0
    migrated.close()


def test_phase_two_lifecycle_schema_is_additive_and_preserves_existing_rows(tmp_path):
    database_path = tmp_path / "state.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE user_profile (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            created_at TEXT NOT NULL
        );
        CREATE TABLE memory (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            scope TEXT NOT NULL,
            persona_id INTEGER,
            relationship_id INTEGER,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            provenance_session_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO user_profile (user_id, timezone, created_at)
        VALUES (42, 'UTC', 'now');
        INSERT INTO memory (
            id, user_id, scope, kind, content, created_at, updated_at
        ) VALUES (11, 42, 'shared', 'fact', 'Keep me', 'now', 'now');
        PRAGMA user_version = 4;
        """
    )
    connection.commit()
    connection.close()

    migrated = open_state_database(database_path)
    tables = {
        row[0]
        for row in migrated.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    profile = migrated.execute(
        "SELECT user_id, lifecycle_epoch, lifecycle_state FROM user_profile"
    ).fetchone()
    memory = migrated.execute(
        """
        SELECT id, version, source, category, confidence, provenance_turn_id
        FROM memory WHERE id = 11
        """
    ).fetchone()

    assert migrated.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert {"session_summary", "memory_extraction_run", "memory_revision", "security_audit"} <= tables
    assert tuple(profile) == (42, 1, "active")
    assert tuple(memory) == (11, 1, "manual", None, None, None)
    migrated.close()


def test_failed_migration_rolls_back_ddl_and_does_not_advance_version(tmp_path):
    database_path = tmp_path / "state.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE conversation (
            id INTEGER PRIMARY KEY,
            platform TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            active_persona_key TEXT NOT NULL,
            UNIQUE(platform, chat_id)
        );
        CREATE TABLE persona_session (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversation(id),
            persona_key TEXT NOT NULL,
            identity_version INTEGER NOT NULL,
            UNIQUE(conversation_id, persona_key, identity_version)
        );
        CREATE TABLE memory (id INTEGER PRIMARY KEY);
        PRAGMA user_version = 1;
        """
    )
    connection.close()

    with pytest.raises(sqlite3.OperationalError):
        open_state_database(database_path)

    check = sqlite3.connect(database_path)
    tables = {
        row[0]
        for row in check.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    version = check.execute("PRAGMA user_version").fetchone()[0]
    check.close()

    assert version == 1
    assert "memory" in tables
    assert "persona" not in tables
    assert "relationship" not in tables
