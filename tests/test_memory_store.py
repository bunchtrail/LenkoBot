import sqlite3

import pytest

from lenkobot.memory import (
    MemoryLimits,
    MemoryScope,
    NewMemory,
    RelationshipVersionConflict,
    SQLiteMemoryStore,
)
from lenkobot.personas import Persona


def persona(key):
    return Persona(
        key=key,
        display_name=key.title(),
        identity_prompt=f"Identity for {key}.",
        identity_version=1,
    )


def contents(records):
    return tuple(record.content for record in records)


def test_memory_acl_filters_by_user_and_active_persona_in_sql(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "state.db")
    companion_id = store.register_persona(persona("companion"))
    analyst_id = store.register_persona(persona("analyst"))

    store.create(
        NewMemory(user_id=42, scope=MemoryScope.SHARED, kind="fact", content="shared-42")
    )
    store.create(
        NewMemory(
            user_id=42,
            scope=MemoryScope.PERSONA_PRIVATE,
            persona_id=companion_id,
            kind="fact",
            content="companion-private",
        )
    )
    store.create(
        NewMemory(
            user_id=42,
            scope=MemoryScope.PERSONA_PRIVATE,
            persona_id=analyst_id,
            kind="fact",
            content="analyst-private",
        )
    )
    companion_relationship = store.ensure_relationship(
        user_id=42,
        persona_id=companion_id,
    )
    analyst_relationship = store.ensure_relationship(user_id=42, persona_id=analyst_id)
    store.create(
        NewMemory(
            user_id=42,
            scope=MemoryScope.RELATIONSHIP,
            relationship_id=companion_relationship.id,
            kind="event",
            content="companion-relationship",
        )
    )
    store.create(
        NewMemory(
            user_id=42,
            scope=MemoryScope.RELATIONSHIP,
            relationship_id=analyst_relationship.id,
            kind="event",
            content="analyst-relationship",
        )
    )
    other_relationship = store.ensure_relationship(user_id=99, persona_id=companion_id)
    store.create(
        NewMemory(user_id=99, scope=MemoryScope.SHARED, kind="fact", content="shared-99")
    )
    store.create(
        NewMemory(
            user_id=99,
            scope=MemoryScope.RELATIONSHIP,
            relationship_id=other_relationship.id,
            kind="event",
            content="other-relationship",
        )
    )

    companion = store.list_for_context(user_id=42, persona_id=companion_id)
    analyst = store.list_for_context(user_id=42, persona_id=analyst_id)
    other_user = store.list_for_context(user_id=99, persona_id=companion_id)

    assert contents(companion.shared) == ("shared-42",)
    assert contents(companion.persona_private) == ("companion-private",)
    assert contents(companion.relationship) == ("companion-relationship",)
    assert contents(analyst.shared) == ("shared-42",)
    assert contents(analyst.persona_private) == ("analyst-private",)
    assert contents(analyst.relationship) == ("analyst-relationship",)
    assert contents(other_user.shared) == ("shared-99",)
    assert contents(other_user.persona_private) == ()
    assert contents(other_user.relationship) == ("other-relationship",)


def test_memory_scope_shape_and_relationship_owner_are_enforced_by_sql(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "state.db")
    companion_id = store.register_persona(persona("companion"))
    relationship = store.ensure_relationship(user_id=42, persona_id=companion_id)

    invalid_records = (
        NewMemory(
            user_id=42,
            scope=MemoryScope.SHARED,
            persona_id=companion_id,
            kind="fact",
            content="invalid shared",
        ),
        NewMemory(
            user_id=42,
            scope=MemoryScope.PERSONA_PRIVATE,
            kind="fact",
            content="invalid private",
        ),
        NewMemory(
            user_id=42,
            scope=MemoryScope.RELATIONSHIP,
            kind="fact",
            content="invalid relationship",
        ),
        NewMemory(
            user_id=99,
            scope=MemoryScope.RELATIONSHIP,
            relationship_id=relationship.id,
            kind="fact",
            content="wrong owner",
        ),
    )

    for record in invalid_records:
        with pytest.raises(sqlite3.IntegrityError):
            store.create(record)


def test_memory_persists_and_can_be_updated_promoted_and_physically_deleted(tmp_path):
    database_path = tmp_path / "state.db"
    first_store = SQLiteMemoryStore(database_path)
    companion_id = first_store.register_persona(persona("companion"))
    record = first_store.create(
        NewMemory(
            user_id=42,
            scope=MemoryScope.PERSONA_PRIVATE,
            persona_id=companion_id,
            kind="preference",
            content="old value",
        )
    )
    first_store.close()

    reopened = SQLiteMemoryStore(database_path)
    assert reopened.persona_id_for_key("companion") == companion_id
    updated = reopened.update(
        record.id,
        user_id=42,
        content="new value",
        kind="fact",
    )
    promoted = reopened.promote_to_shared(updated.id, user_id=42)

    assert promoted.scope is MemoryScope.SHARED
    assert promoted.persona_id is None
    assert contents(
        reopened.list_for_context(user_id=42, persona_id=companion_id).shared
    ) == ("new value",)
    assert reopened.delete(promoted.id, user_id=99) is False
    assert reopened.delete(promoted.id, user_id=42) is True
    assert reopened.get(promoted.id, user_id=42) is None
    assert reopened.memory_count() == 0


def test_memory_update_cannot_bypass_kind_and_content_constraints(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "state.db")
    record = store.create(
        NewMemory(
            user_id=42,
            scope=MemoryScope.SHARED,
            kind="fact",
            content="valid",
        )
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.update(record.id, user_id=42, content="updated", kind="")
    with pytest.raises(sqlite3.IntegrityError):
        store.update(record.id, user_id=42, content="   ")

    unchanged = store.get(record.id, user_id=42)
    assert unchanged.kind == "fact"
    assert unchanged.content == "valid"


def test_relationship_update_increments_version_and_rejects_stale_write(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "state.db")
    companion_id = store.register_persona(persona("companion"))
    relationship = store.ensure_relationship(user_id=42, persona_id=companion_id)

    updated = store.update_relationship(
        user_id=42,
        persona_id=companion_id,
        summary="Prefers concise answers.",
        state_json='{"tone":"direct"}',
        expected_version=relationship.version,
    )

    assert updated.version == relationship.version + 1
    assert updated.summary == "Prefers concise answers."
    assert updated.state_json == '{"tone":"direct"}'
    with pytest.raises(RelationshipVersionConflict):
        store.update_relationship(
            user_id=42,
            persona_id=companion_id,
            summary="stale",
            state_json="{}",
            expected_version=relationship.version,
        )


def test_context_limits_use_deterministic_updated_order_across_scopes(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "state.db")
    companion_id = store.register_persona(persona("companion"))
    relationship = store.ensure_relationship(user_id=42, persona_id=companion_id)
    records = (
        NewMemory(
            user_id=42,
            scope=MemoryScope.SHARED,
            kind="fact",
            content="shared-old",
            updated_at="2026-07-17T10:00:00+00:00",
        ),
        NewMemory(
            user_id=42,
            scope=MemoryScope.RELATIONSHIP,
            relationship_id=relationship.id,
            kind="event",
            content="relationship",
            updated_at="2026-07-17T11:00:00+00:00",
        ),
        NewMemory(
            user_id=42,
            scope=MemoryScope.PERSONA_PRIVATE,
            persona_id=companion_id,
            kind="fact",
            content="private",
            updated_at="2026-07-17T12:00:00+00:00",
        ),
        NewMemory(
            user_id=42,
            scope=MemoryScope.SHARED,
            kind="fact",
            content="shared-new",
            updated_at="2026-07-17T13:00:00+00:00",
        ),
    )
    for record in records:
        store.create(record)

    context = store.list_for_context(
        user_id=42,
        persona_id=companion_id,
        limits=MemoryLimits(shared=1, persona_private=1, relationship=1, total=2),
    )

    assert contents(context.shared) == ("shared-new",)
    assert contents(context.persona_private) == ("private",)
    assert contents(context.relationship) == ()
