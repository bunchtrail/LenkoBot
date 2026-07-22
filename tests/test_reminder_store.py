from datetime import datetime, timedelta, timezone

import pytest

from lenkobot.reminder_schedule import RecurrenceRule, ScheduleKind
from lenkobot.reminder_store import (
    ReminderDraft,
    ReminderJobStatus,
    SQLiteReminderStore,
    TaskStatus,
)
from lenkobot.sqlite_schema import CURRENT_SCHEMA_VERSION, open_state_database


_NOW = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)


def setup_owner(database_path):
    connection = open_state_database(database_path)
    with connection:
        connection.execute(
            "INSERT INTO user_profile (user_id, created_at) VALUES (42, 'now')"
        )
        cursor = connection.execute(
            """
            INSERT INTO persona (
                profile_id, key, display_name, identity_prompt, identity_version
            ) VALUES ('default', 'companion', 'Companion', 'Prompt', 1)
            """
        )
    persona_id = int(cursor.lastrowid)
    connection.close()
    return persona_id


def draft(persona_id, **overrides):
    values = {
        "owner_user_id": 42,
        "persona_id": persona_id,
        "chat_id": 500,
        "text": "Call the clinic",
        "local_start": datetime(2026, 7, 22, 12, 30),
        "timezone_name": "Europe/Berlin",
        "recurrence": RecurrenceRule(kind=ScheduleKind.DAILY, count=3),
    }
    values.update(overrides)
    return ReminderDraft(**values)


def test_reminder_schema_is_additive_and_profile_policy_has_safe_defaults(tmp_path):
    connection = open_state_database(tmp_path / "state.db")

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    profile = connection.execute(
        """
        SELECT timezone, quiet_start_minute, quiet_end_minute
        FROM user_profile
        """
    ).fetchone()

    assert connection.execute("PRAGMA user_version").fetchone()[0] == (
        CURRENT_SCHEMA_VERSION
    )
    assert {
        "task",
        "reminder_job",
        "reminder_run",
        "delivery_outbox",
        "reminder_delivery_audit",
    } <= tables
    assert profile is None
    connection.close()


def test_draft_requires_explicit_activation_and_survives_restart(tmp_path):
    database_path = tmp_path / "state.db"
    persona_id = setup_owner(database_path)
    store = SQLiteReminderStore(database_path, clock=lambda: _NOW)

    created = store.create_draft(draft(persona_id))

    assert created.task.status is TaskStatus.AWAITING_CONFIRMATION
    assert created.task.persona_id == persona_id
    assert created.job.status is ReminderJobStatus.DRAFT
    assert created.job.next_scheduled_for is None

    awaiting = store.mark_awaiting_confirmation(
        task_id=created.task.id,
        owner_user_id=42,
    )
    assert awaiting.status is ReminderJobStatus.AWAITING_CONFIRMATION

    active = store.activate(task_id=created.task.id, owner_user_id=42)
    assert active.status is ReminderJobStatus.ACTIVE
    assert active.next_scheduled_for == datetime(
        2026,
        7,
        22,
        10,
        30,
        tzinfo=timezone.utc,
    )
    assert store.activate(task_id=created.task.id, owner_user_id=42) == active
    store.close()

    reopened = SQLiteReminderStore(database_path, clock=lambda: _NOW)
    task_record = reopened.get_task(task_id=created.task.id, owner_user_id=42)
    job = reopened.get_job(job_id=created.job.id, owner_user_id=42)

    assert task_record is not None
    assert task_record.status is TaskStatus.ACTIVE
    assert task_record.text == "Call the clinic"
    assert job == active
    assert job.recurrence == RecurrenceRule(kind=ScheduleKind.DAILY, count=3)
    assert reopened.get_task(task_id=created.task.id, owner_user_id=99) is None
    reopened.close()


@pytest.mark.parametrize(
    "local_start",
    (
        datetime(2026, 3, 29, 2, 30),
        datetime(2026, 10, 25, 2, 30),
    ),
)
def test_activation_persists_dst_invalid_schedule_as_needs_review(
    tmp_path,
    local_start,
):
    database_path = tmp_path / "state.db"
    persona_id = setup_owner(database_path)
    store = SQLiteReminderStore(database_path, clock=lambda: _NOW)
    created = store.create_draft(
        draft(
            persona_id,
            local_start=local_start,
            recurrence=RecurrenceRule(kind=ScheduleKind.ONCE),
        )
    )
    store.mark_awaiting_confirmation(
        task_id=created.task.id,
        owner_user_id=42,
    )

    job = store.activate(task_id=created.task.id, owner_user_id=42)
    task_record = store.get_task(task_id=created.task.id, owner_user_id=42)

    assert job.status is ReminderJobStatus.NEEDS_REVIEW
    assert job.next_scheduled_for is None
    assert task_record is not None
    assert task_record.status is TaskStatus.NEEDS_REVIEW
    store.close()


def test_profile_timezone_and_quiet_hours_are_validated_and_persisted(tmp_path):
    database_path = tmp_path / "state.db"
    setup_owner(database_path)
    store = SQLiteReminderStore(database_path, clock=lambda: _NOW)

    initial = store.get_profile_policy(owner_user_id=42)
    changed = store.set_profile_policy(
        owner_user_id=42,
        timezone_name="Asia/Yekaterinburg",
        quiet_start_minute=23 * 60,
        quiet_end_minute=7 * 60,
    )

    assert initial.timezone_name == "UTC"
    assert initial.quiet_start_minute is None
    assert initial.quiet_end_minute is None
    assert changed.timezone_name == "Asia/Yekaterinburg"
    assert changed.quiet_start_minute == 1380
    assert changed.quiet_end_minute == 420
    with pytest.raises(ValueError):
        store.set_profile_policy(
            owner_user_id=42,
            timezone_name="Not/AZone",
            quiet_start_minute=None,
            quiet_end_minute=420,
        )
    store.close()

    reopened = SQLiteReminderStore(database_path, clock=lambda: _NOW)
    assert reopened.get_profile_policy(owner_user_id=42) == changed
    reopened.close()


def test_cancel_complete_and_reset_purge_are_owner_scoped_and_idempotent(tmp_path):
    database_path = tmp_path / "state.db"
    persona_id = setup_owner(database_path)
    store = SQLiteReminderStore(database_path, clock=lambda: _NOW)
    first = store.create_draft(draft(persona_id, text="First"))
    second = store.create_draft(draft(persona_id, text="Second"))
    for item in (first, second):
        store.mark_awaiting_confirmation(task_id=item.task.id, owner_user_id=42)
        store.activate(task_id=item.task.id, owner_user_id=42)

    cancelled = store.cancel_task(task_id=first.task.id, owner_user_id=42)
    completed = store.complete_task(task_id=second.task.id, owner_user_id=42)

    assert cancelled.status is TaskStatus.CANCELLED
    assert store.cancel_task(task_id=first.task.id, owner_user_id=42) == cancelled
    assert completed.status is TaskStatus.COMPLETED
    assert store.complete_task(task_id=second.task.id, owner_user_id=42) == completed
    with pytest.raises(PermissionError):
        store.cancel_task(task_id=first.task.id, owner_user_id=99)

    connection = open_state_database(database_path)
    with connection:
        connection.execute(
            """
            UPDATE user_profile
            SET lifecycle_epoch = 2, lifecycle_state = 'reset_in_progress'
            WHERE user_id = 42
            """
        )
    connection.close()
    store.purge_owner(owner_user_id=42, lifecycle_epoch=2)

    assert store.list_tasks(owner_user_id=42) == ()
    store.close()


def test_snooze_creates_one_deduplicated_run_without_moving_recurrence_cursor(
    tmp_path,
):
    database_path = tmp_path / "state.db"
    persona_id = setup_owner(database_path)
    store = SQLiteReminderStore(database_path, clock=lambda: _NOW)
    created = store.create_draft(draft(persona_id))
    store.mark_awaiting_confirmation(task_id=created.task.id, owner_user_id=42)
    active = store.activate(task_id=created.task.id, owner_user_id=42)

    first = store.snooze_task(
        task_id=created.task.id,
        owner_user_id=42,
        action_token="confirmation-token",
        delay_seconds=600,
    )
    replay = store.snooze_task(
        task_id=created.task.id,
        owner_user_id=42,
        action_token="confirmation-token",
        delay_seconds=600,
    )

    assert replay == first
    assert first.scheduled_for == _NOW + timedelta(minutes=10)
    assert store.list_runs(job_id=created.job.id, owner_user_id=42) == (first,)
    outbox = store.get_outbox(run_id=first.id, owner_user_id=42)
    assert outbox is not None
    assert outbox.scheduled_for == first.scheduled_for
    assert outbox.available_at == first.scheduled_for
    assert store.get_job(job_id=created.job.id, owner_user_id=42) == active
    store.close()
