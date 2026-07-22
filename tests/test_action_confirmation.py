from datetime import datetime, timedelta, timezone

from lenkobot.action_confirmation import (
    ActionConfirmationService,
    ConfirmationOutcome,
    SQLiteActionConfirmationStore,
)


_START = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, now=_START):
        self.now = now

    def __call__(self):
        return self.now


def build_store(tmp_path, clock=None, ttl_seconds=300):
    return SQLiteActionConfirmationStore(
        tmp_path / "state.db",
        ttl_seconds=ttl_seconds,
        clock=clock,
    )


def test_consume_returns_action_exactly_once(tmp_path):
    store = build_store(tmp_path, clock=MutableClock())
    token = store.create(
        owner_user_id=42,
        action_type="forget_memory",
        payload={"memory_id": 7},
    )

    action = store.consume(token=token, owner_user_id=42)

    assert action is not None
    assert action.token == token
    assert action.action_type == "forget_memory"
    assert action.payload == {"memory_id": 7}
    assert store.consume(token=token, owner_user_id=42) is None
    store.close()


def test_consume_rejects_foreign_owner_without_consuming_receipt(tmp_path):
    store = build_store(tmp_path, clock=MutableClock())
    token = store.create(
        owner_user_id=42,
        action_type="forget_memory",
        payload={"memory_id": 7},
    )

    assert store.consume(token=token, owner_user_id=99) is None
    assert store.consume(token=token, owner_user_id=42) is not None
    store.close()


def test_consume_rejects_expired_receipt(tmp_path):
    clock = MutableClock()
    store = build_store(tmp_path, clock=clock, ttl_seconds=300)
    token = store.create(
        owner_user_id=42,
        action_type="close_session",
        payload={"session_id": 3},
    )

    clock.now = _START + timedelta(seconds=301)

    assert store.consume(token=token, owner_user_id=42) is None
    store.close()


def test_consume_rejects_tampered_payload(tmp_path):
    store = build_store(tmp_path, clock=MutableClock())
    token = store.create(
        owner_user_id=42,
        action_type="forget_memory",
        payload={"memory_id": 7},
    )
    store._connection.execute(
        "UPDATE action_confirmation SET payload_json = ? WHERE token = ?",
        ('{"memory_id": 8}', token),
    )
    store._connection.commit()

    assert store.consume(token=token, owner_user_id=42) is None
    store.close()


def test_unknown_token_returns_none(tmp_path):
    store = build_store(tmp_path, clock=MutableClock())

    assert store.consume(token="missing", owner_user_id=42) is None
    store.close()


def test_tokens_are_unique_and_expired_rows_are_cleaned_up(tmp_path):
    clock = MutableClock()
    store = build_store(tmp_path, clock=clock, ttl_seconds=60)
    first = store.create(owner_user_id=42, action_type="close_session", payload={})
    clock.now = _START + timedelta(seconds=120)
    second = store.create(owner_user_id=42, action_type="close_session", payload={})

    assert first != second
    remaining = store._connection.execute(
        "SELECT COUNT(*) FROM action_confirmation WHERE owner_user_id = 42"
    ).fetchone()[0]
    assert remaining == 1
    store.close()


def test_store_rejects_invalid_constructor_and_create_arguments(tmp_path):
    clock = MutableClock()
    try:
        SQLiteActionConfirmationStore(tmp_path / "state.db", ttl_seconds=0)
    except ValueError:
        pass
    else:
        raise AssertionError("non-positive ttl must be rejected")

    store = build_store(tmp_path, clock=clock)
    for bad_owner in (0, -1, True, "42"):
        try:
            store.create(owner_user_id=bad_owner, action_type="x", payload={})
        except ValueError:
            pass
        else:
            raise AssertionError("invalid owner must be rejected")
    try:
        store.create(owner_user_id=42, action_type="  ", payload={})
    except ValueError:
        pass
    else:
        raise AssertionError("blank action type must be rejected")
    store.close()


def test_resolution_outcome_is_durable_and_same_decision_can_be_recovered(tmp_path):
    store = build_store(tmp_path, clock=MutableClock())
    token = store.create(
        owner_user_id=42,
        action_type="activate_reminder",
        payload={"task_id": 7},
    )

    first = store.resolve(
        token=token,
        owner_user_id=42,
        outcome=ConfirmationOutcome.CONFIRMED,
    )
    replay = store.resolve(
        token=token,
        owner_user_id=42,
        outcome=ConfirmationOutcome.CONFIRMED,
    )

    assert first is not None
    assert first.first_resolution is True
    assert first.outcome is ConfirmationOutcome.CONFIRMED
    assert first.action.payload == {"task_id": 7}
    assert replay is not None
    assert replay.first_resolution is False
    assert replay.action == first.action
    assert store.resolve(
        token=token,
        owner_user_id=42,
        outcome=ConfirmationOutcome.CANCELLED,
    ) is None
    row = store._connection.execute(
        "SELECT outcome, resolved_at FROM action_confirmation WHERE token = ?",
        (token,),
    ).fetchone()
    assert tuple(row) == ("confirmed", _START.isoformat(timespec="microseconds"))
    store.close()


def test_action_confirmation_service_records_cancellation_without_execution(tmp_path):
    store = build_store(tmp_path, clock=MutableClock())
    service = ActionConfirmationService(store)
    token = service.request(
        owner_user_id=42,
        action_type="activate_reminder",
        payload={"task_id": 7},
    )

    resolution = service.resolve(
        token=token,
        owner_user_id=42,
        confirmed=False,
    )

    assert resolution is not None
    assert resolution.outcome is ConfirmationOutcome.CANCELLED
    assert resolution.first_resolution is True
    assert store.consume(token=token, owner_user_id=42) is None
    store.close()
