from collections.abc import Callable
from pathlib import Path
import sqlite3


class SchemaVersionError(RuntimeError):
    pass


def _create_conversation_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation (
            id INTEGER PRIMARY KEY,
            platform TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            active_persona_key TEXT NOT NULL,
            UNIQUE(platform, chat_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS persona_session (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversation(id),
            persona_key TEXT NOT NULL,
            identity_version INTEGER NOT NULL,
            UNIQUE(conversation_id, persona_key, identity_version)
        )
        """
    )


def _create_memory_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS persona (
            id INTEGER PRIMARY KEY,
            profile_id TEXT NOT NULL,
            key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            identity_prompt TEXT NOT NULL,
            identity_version INTEGER NOT NULL CHECK(identity_version > 0),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'disabled')),
            UNIQUE(profile_id, key)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS relationship (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            persona_id INTEGER NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
            summary TEXT NOT NULL DEFAULT '',
            state_json TEXT NOT NULL DEFAULT '{}'
                CHECK(json_valid(state_json)),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, persona_id),
            UNIQUE(id, user_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            scope TEXT NOT NULL
                CHECK(scope IN ('shared', 'persona_private', 'relationship')),
            persona_id INTEGER REFERENCES persona(id) ON DELETE CASCADE,
            relationship_id INTEGER,
            kind TEXT NOT NULL CHECK(length(trim(kind)) > 0),
            content TEXT NOT NULL CHECK(length(trim(content)) > 0),
            provenance_session_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'deleted')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (relationship_id, user_id)
                REFERENCES relationship(id, user_id) ON DELETE CASCADE,
            CHECK(
                (scope = 'shared' AND persona_id IS NULL AND relationship_id IS NULL)
                OR
                (scope = 'persona_private' AND persona_id IS NOT NULL
                    AND relationship_id IS NULL)
                OR
                (scope = 'relationship' AND persona_id IS NULL
                    AND relationship_id IS NOT NULL)
            )
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS memory_user_scope_persona_updated_idx
            ON memory(user_id, scope, persona_id, updated_at DESC, id DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS memory_relationship_updated_idx
            ON memory(relationship_id, updated_at DESC, id DESC)
        """
    )


def _add_conversation_version(connection: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(conversation)").fetchall()
    }
    if "version" not in columns:
        connection.execute(
            """
            ALTER TABLE conversation
            ADD COLUMN version INTEGER NOT NULL DEFAULT 0
            """
        )


def _create_session_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS user_profile (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT NOT NULL DEFAULT 'UTC'
                CHECK(length(trim(timezone)) > 0),
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session (
            id INTEGER PRIMARY KEY,
            persona_session_id INTEGER NOT NULL
                REFERENCES persona_session(id) ON DELETE CASCADE,
            owner_user_id INTEGER NOT NULL
                REFERENCES user_profile(user_id) ON DELETE CASCADE,
            generation INTEGER NOT NULL CHECK(generation > 0),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'closed')),
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            UNIQUE(persona_session_id, generation),
            CHECK(
                (status = 'active' AND closed_at IS NULL)
                OR (status = 'closed' AND closed_at IS NOT NULL)
            )
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS session_one_active_lane_idx
            ON session(persona_session_id)
            WHERE status = 'active'
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS transcript_turn (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL CHECK(sequence > 0),
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL CHECK(length(trim(content)) > 0),
            provider_response_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, sequence),
            UNIQUE(id, session_id),
            CHECK(role = 'assistant' OR provider_response_id IS NULL)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS transcript_turn_session_sequence_idx
            ON transcript_turn(session_id, sequence DESC)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS transcript_failure (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
            related_turn_id INTEGER NOT NULL,
            stage TEXT NOT NULL CHECK(stage IN ('provider', 'delivery')),
            error_kind TEXT NOT NULL
                CHECK(length(trim(error_kind)) > 0 AND length(error_kind) <= 100),
            created_at TEXT NOT NULL,
            FOREIGN KEY (related_turn_id, session_id)
                REFERENCES transcript_turn(id, session_id) ON DELETE CASCADE
        )
        """
    )


_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _create_conversation_schema,
    _create_memory_schema,
    _add_conversation_version,
    _create_session_schema,
)
CURRENT_SCHEMA_VERSION = len(_MIGRATIONS)


def open_state_database(database_path: Path | str) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        while True:
            current_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if current_version > CURRENT_SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database schema version {current_version} is newer than supported "
                    f"version {CURRENT_SCHEMA_VERSION}"
                )
            if current_version == CURRENT_SCHEMA_VERSION:
                break
            _apply_migration(connection, current_version + 1)
    except Exception:
        connection.close()
        raise
    return connection


def _apply_migration(connection: sqlite3.Connection, target_version: int) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version >= target_version:
            connection.commit()
            return
        if current_version != target_version - 1:
            raise SchemaVersionError(
                f"cannot apply schema version {target_version} after {current_version}"
            )
        _MIGRATIONS[target_version - 1](connection)
        connection.execute(f"PRAGMA user_version = {target_version}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
