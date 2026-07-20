from collections.abc import Callable
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .personas import Persona, VoicePack, persona_content_hash


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
    columns = _column_names(connection, "conversation")
    if "version" not in columns:
        connection.execute(
            """
            ALTER TABLE conversation
            ADD COLUMN version INTEGER NOT NULL DEFAULT 0
            """
        )


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


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


def _create_phase_two_lifecycle_schema(connection: sqlite3.Connection) -> None:
    # Keep the additive migration safe for sparse legacy fixtures while still
    # failing on incompatible pre-existing tables when indexes are created.
    _create_memory_schema(connection)

    profile_columns = _column_names(connection, "user_profile")
    if "lifecycle_epoch" not in profile_columns:
        connection.execute(
            """
            ALTER TABLE user_profile
            ADD COLUMN lifecycle_epoch INTEGER NOT NULL DEFAULT 1
                CHECK(lifecycle_epoch > 0)
            """
        )
    if "lifecycle_state" not in profile_columns:
        connection.execute(
            """
            ALTER TABLE user_profile
            ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'active'
                CHECK(lifecycle_state IN ('active', 'reset_in_progress'))
            """
        )

    memory_columns = _column_names(connection, "memory")
    additions = {
        "version": "INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)",
        "source": (
            "TEXT NOT NULL DEFAULT 'manual' "
            "CHECK(source IN ('manual', 'automatic'))"
        ),
        "category": (
            "TEXT CHECK(category IS NULL OR length(trim(category)) > 0)"
        ),
        "confidence": (
            "REAL CHECK(confidence IS NULL OR "
            "(confidence >= 0.0 AND confidence <= 1.0))"
        ),
        "provenance_turn_id": "INTEGER",
    }
    for name, definition in additions.items():
        if name not in memory_columns:
            connection.execute(f"ALTER TABLE memory ADD COLUMN {name} {definition}")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_summary (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL UNIQUE
                REFERENCES session(id) ON DELETE CASCADE,
            owner_user_id INTEGER NOT NULL
                REFERENCES user_profile(user_id) ON DELETE CASCADE,
            content TEXT NOT NULL CHECK(length(trim(content)) > 0),
            source_turn_count INTEGER NOT NULL CHECK(source_turn_count >= 0),
            lifecycle_epoch INTEGER NOT NULL CHECK(lifecycle_epoch > 0),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'invalidated')),
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_extraction_run (
            id INTEGER PRIMARY KEY,
            owner_user_id INTEGER NOT NULL
                REFERENCES user_profile(user_id) ON DELETE CASCADE,
            session_id INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
            source_turn_id INTEGER NOT NULL,
            lifecycle_epoch INTEGER NOT NULL CHECK(lifecycle_epoch > 0),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN (
                    'pending', 'processing', 'completed', 'failed', 'discarded'
                )),
            attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
            error_kind TEXT CHECK(
                error_kind IS NULL OR (
                    length(trim(error_kind)) > 0 AND length(error_kind) <= 100
                )
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(session_id, source_turn_id, lifecycle_epoch)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS memory_extraction_run_session_status_idx
            ON memory_extraction_run(session_id, status, id)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_revision (
            id INTEGER PRIMARY KEY,
            memory_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
            owner_user_id INTEGER NOT NULL
                REFERENCES user_profile(user_id) ON DELETE CASCADE,
            version INTEGER NOT NULL CHECK(version > 0),
            content TEXT NOT NULL CHECK(length(trim(content)) > 0),
            category TEXT CHECK(
                category IS NULL OR length(trim(category)) > 0
            ),
            confidence REAL CHECK(
                confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)
            ),
            changed_at TEXT NOT NULL,
            UNIQUE(memory_id, version)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS security_audit (
            id INTEGER PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            lifecycle_epoch INTEGER NOT NULL CHECK(lifecycle_epoch > 0),
            event_type TEXT NOT NULL CHECK(
                event_type IN ('reset_completed')
            ),
            created_at TEXT NOT NULL
        )
        """
    )


def _create_persona_version_schema(connection: sqlite3.Connection) -> None:
    tables = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "persona" not in tables:
        _create_memory_schema(connection)
    if "persona_session" not in tables:
        _create_conversation_schema(connection)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS persona_version (
            id INTEGER PRIMARY KEY,
            persona_id INTEGER NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
            identity_version INTEGER NOT NULL CHECK(identity_version > 0),
            display_name TEXT NOT NULL,
            identity_prompt TEXT NOT NULL,
            voice_json TEXT NOT NULL CHECK(json_valid(voice_json)),
            content_hash TEXT NOT NULL CHECK(length(trim(content_hash)) > 0),
            created_at TEXT NOT NULL,
            UNIQUE(persona_id, identity_version),
            UNIQUE(persona_id, content_hash)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS persona_version_persona_created_idx
            ON persona_version(persona_id, created_at DESC, id DESC)
        """
    )

    legacy_created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    empty_voice = VoicePack()
    for row in connection.execute(
        """
        SELECT id, key, display_name, identity_prompt, identity_version
        FROM persona
        """
    ).fetchall():
        persona = Persona(
            key=str(row["key"]),
            display_name=str(row["display_name"]),
            identity_prompt=str(row["identity_prompt"]),
            identity_version=int(row["identity_version"]),
            voice=empty_voice,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO persona_version (
                persona_id, identity_version, display_name, identity_prompt,
                voice_json, content_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(row["id"]),
                persona.identity_version,
                persona.display_name,
                persona.identity_prompt,
                json.dumps(
                    empty_voice.as_dict(),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                persona_content_hash(persona),
                legacy_created_at,
            ),
        )

    columns = _column_names(connection, "persona_session")
    if "persona_version_id" not in columns:
        connection.execute(
            """
            ALTER TABLE persona_session
            ADD COLUMN persona_version_id INTEGER REFERENCES persona_version(id)
            """
        )
    connection.execute(
        """
        UPDATE persona_session
        SET persona_version_id = (
            SELECT version.id
            FROM persona_version AS version
            JOIN persona ON persona.id = version.persona_id
            WHERE persona.profile_id = 'default'
                AND persona.key = persona_session.persona_key
                AND version.identity_version = persona_session.identity_version
        )
        WHERE persona_version_id IS NULL
        """
    )


def _create_action_confirmation_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS action_confirmation (
            token TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            action_type TEXT NOT NULL CHECK(length(trim(action_type)) > 0),
            payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
            payload_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS action_confirmation_owner_expiry_idx
            ON action_confirmation(owner_user_id, expires_at)
        """
    )


_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _create_conversation_schema,
    _create_memory_schema,
    _add_conversation_version,
    _create_session_schema,
    _create_phase_two_lifecycle_schema,
    _create_persona_version_schema,
    _create_action_confirmation_schema,
)
CURRENT_SCHEMA_VERSION = len(_MIGRATIONS)


def open_state_database(database_path: Path | str) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, check_same_thread=False)
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
