import pytest

from lenkobot.memory import (
    MemoryCategory,
    MemoryScope,
    MemorySource,
    NewMemory,
    SQLiteMemoryStore,
)
from lenkobot.reset_coordinator import ResetCoordinator
from lenkobot.sqlite_schema import open_state_database


def setup_owner(database_path):
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
            ) VALUES (1, 1, 1, 'user', 'raw', 'now')
            """
        )
        connection.execute(
            """
            INSERT INTO memory_extraction_run (
                id, owner_user_id, session_id, source_turn_id, lifecycle_epoch,
                status, attempt, created_at, updated_at
            ) VALUES (1, 42, 1, 1, 1, 'pending', 0, 'now', 'now')
            """
        )
        connection.execute(
            """
            INSERT INTO security_audit (
                owner_user_id, lifecycle_epoch, event_type, created_at
            ) VALUES (42, 1, 'reset_completed', 'old')
            """
        )
    connection.close()


def test_reset_fences_epoch_purges_owner_data_and_replaces_audit(tmp_path):
    database_path = tmp_path / "state.db"
    setup_owner(database_path)
    memory_store = SQLiteMemoryStore(database_path)
    config_path = tmp_path / "config.toml"
    oauth_state_path = tmp_path / "oauth.state"
    config_path.write_text("configured", encoding="ascii")
    oauth_state_path.write_text("credential-state", encoding="ascii")
    memory_store.create(
        NewMemory(
            user_id=42,
            scope=MemoryScope.SHARED,
            kind="fact",
            content="remove me",
            source=MemorySource.AUTOMATIC,
            category=MemoryCategory.FACT,
            confidence=0.5,
            provenance_turn_id=1,
        )
    )
    seen = []
    coordinator = ResetCoordinator(database_path, required_hooks=("memory",))
    coordinator.register_purge_hook("memory", lambda owner, epoch: seen.append((owner, epoch)))

    result = coordinator.reset(owner_user_id=42)

    assert result.previous_epoch == 1
    assert result.lifecycle_epoch == 2
    assert seen == [(42, 2)]
    check = open_state_database(database_path)
    assert check.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 0
    assert check.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 0
    assert check.execute("SELECT COUNT(*) FROM transcript_turn").fetchone()[0] == 0
    assert check.execute("SELECT COUNT(*) FROM memory_extraction_run").fetchone()[0] == 0
    profile = check.execute(
        "SELECT lifecycle_epoch, lifecycle_state FROM user_profile WHERE user_id = 42"
    ).fetchone()
    assert (profile[0], profile[1]) == (2, "active")
    audit = check.execute(
        "SELECT lifecycle_epoch, event_type FROM security_audit WHERE owner_user_id = 42"
    ).fetchall()
    assert [(row[0], row[1]) for row in audit] == [(2, "reset_completed")]
    check.close()
    assert config_path.read_text(encoding="ascii") == "configured"
    assert oauth_state_path.read_text(encoding="ascii") == "credential-state"


def test_reset_requires_all_registered_purge_hooks_before_fencing(tmp_path):
    database_path = tmp_path / "state.db"
    setup_owner(database_path)
    coordinator = ResetCoordinator(database_path, required_hooks=("memory", "tasks"))

    with pytest.raises(RuntimeError, match="tasks"):
        coordinator.reset(owner_user_id=42)

    check = open_state_database(database_path)
    profile = check.execute(
        "SELECT lifecycle_epoch, lifecycle_state FROM user_profile WHERE user_id = 42"
    ).fetchone()
    assert (profile[0], profile[1]) == (1, "active")
    check.close()


def test_stale_extraction_result_cannot_activate_after_epoch_change(tmp_path):
    database_path = tmp_path / "state.db"
    setup_owner(database_path)
    memory_store = SQLiteMemoryStore(database_path)
    run = memory_store.ensure_extraction_run(
        owner_user_id=42,
        session_id=1,
        source_turn_id=1,
    )
    memory_store.claim_extraction_run(run.id, owner_user_id=42)
    connection = open_state_database(database_path)
    with connection:
        connection.execute(
            "UPDATE user_profile SET lifecycle_epoch = 2 WHERE user_id = 42"
        )
    connection.close()

    with pytest.raises(RuntimeError, match="stale"):
        memory_store.activate_extraction(
            run.id,
            owner_user_id=42,
            memories=(
                NewMemory(
                    user_id=42,
                    scope=MemoryScope.SHARED,
                    kind="fact",
                    content="stale",
                    source=MemorySource.AUTOMATIC,
                    category=MemoryCategory.FACT,
                    confidence=0.9,
                    provenance_session_id=1,
                    provenance_turn_id=1,
                ),
            ),
        )

    assert memory_store.memory_count() == 0
