from datetime import datetime, timedelta, timezone

from lenkobot.reminder_schedule import RecurrenceRule, ScheduleKind
from lenkobot.reminder_store import (
    OutboxStatus,
    ReminderDraft,
    ReminderJobStatus,
    ReminderRunStatus,
    SQLiteReminderStore,
    TaskStatus,
)
from lenkobot.sqlite_schema import open_state_database


class MutableClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


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


def create_active(
    store,
    persona_id,
    *,
    local_start,
    timezone_name="UTC",
    recurrence=None,
    grace_seconds=3600,
    quiet_start_minute=None,
    quiet_end_minute=None,
):
    created = store.create_draft(
        ReminderDraft(
            owner_user_id=42,
            persona_id=persona_id,
            chat_id=500,
            text="Take medicine",
            local_start=local_start,
            timezone_name=timezone_name,
            recurrence=recurrence or RecurrenceRule(kind=ScheduleKind.ONCE),
            grace_seconds=grace_seconds,
            quiet_start_minute=quiet_start_minute,
            quiet_end_minute=quiet_end_minute,
        )
    )
    store.mark_awaiting_confirmation(task_id=created.task.id, owner_user_id=42)
    store.activate(task_id=created.task.id, owner_user_id=42)
    return created


def test_scheduler_materializes_one_logical_run_and_advances_cursor_atomically(tmp_path):
    due = datetime(2026, 7, 22, 10, 30, tzinfo=timezone.utc)
    clock = MutableClock(due)
    database_path = tmp_path / "state.db"
    persona_id = setup_owner(database_path)
    store = SQLiteReminderStore(database_path, clock=clock)
    created = create_active(
        store,
        persona_id,
        local_start=datetime(2026, 7, 22, 12, 30),
        timezone_name="Europe/Berlin",
        recurrence=RecurrenceRule(kind=ScheduleKind.DAILY, count=2),
    )

    first = store.materialize_due()
    duplicate = store.materialize_due()
    runs = store.list_runs(job_id=created.job.id, owner_user_id=42)
    job = store.get_job(job_id=created.job.id, owner_user_id=42)
    outbox = store.get_outbox(run_id=first[0].id, owner_user_id=42)

    assert first == runs
    assert duplicate == ()
    assert len(runs) == 1
    assert runs[0].status is ReminderRunStatus.DUE
    assert runs[0].scheduled_for == due
    assert outbox is not None
    assert outbox.status is OutboxStatus.PENDING
    assert outbox.scheduled_for == due
    assert outbox.available_at == due
    assert job is not None
    assert job.emitted_count == 1
    assert job.next_scheduled_for == due + timedelta(days=1)
    store.close()


def test_scheduler_records_missed_run_and_cancelled_outbox_after_grace(tmp_path):
    scheduled = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    clock = MutableClock(scheduled + timedelta(hours=2))
    database_path = tmp_path / "state.db"
    persona_id = setup_owner(database_path)
    store = SQLiteReminderStore(database_path, clock=clock)
    created = create_active(
        store,
        persona_id,
        local_start=scheduled.replace(tzinfo=None),
        grace_seconds=3600,
    )

    run = store.materialize_due()[0]
    outbox = store.get_outbox(run_id=run.id, owner_user_id=42)
    job = store.get_job(job_id=created.job.id, owner_user_id=42)

    assert run.status is ReminderRunStatus.MISSED
    assert outbox is not None
    assert outbox.status is OutboxStatus.CANCELLED
    assert job is not None
    assert job.status is ReminderJobStatus.COMPLETED
    assert store.lease_delivery() is None
    store.close()


def test_quiet_hours_delay_outbox_without_changing_scheduled_instant(tmp_path):
    scheduled = datetime(2026, 7, 22, 21, 30, tzinfo=timezone.utc)
    clock = MutableClock(scheduled)
    database_path = tmp_path / "state.db"
    persona_id = setup_owner(database_path)
    store = SQLiteReminderStore(database_path, clock=clock)
    create_active(
        store,
        persona_id,
        local_start=datetime(2026, 7, 22, 23, 30),
        timezone_name="Europe/Berlin",
        grace_seconds=12 * 3600,
        quiet_start_minute=22 * 60,
        quiet_end_minute=7 * 60,
    )

    run = store.materialize_due()[0]
    outbox = store.get_outbox(run_id=run.id, owner_user_id=42)

    assert outbox is not None
    assert outbox.scheduled_for == scheduled
    assert outbox.available_at == datetime(
        2026,
        7,
        23,
        5,
        0,
        tzinfo=timezone.utc,
    )
    assert store.lease_delivery() is None
    clock.now = outbox.available_at
    assert store.lease_delivery() is not None
    store.close()


def test_recurring_cursor_stops_at_dst_invalid_occurrence(tmp_path):
    scheduled = datetime(2026, 3, 28, 1, 30, tzinfo=timezone.utc)
    clock = MutableClock(scheduled)
    database_path = tmp_path / "state.db"
    persona_id = setup_owner(database_path)
    store = SQLiteReminderStore(database_path, clock=clock)
    created = create_active(
        store,
        persona_id,
        local_start=datetime(2026, 3, 28, 2, 30),
        timezone_name="Europe/Berlin",
        recurrence=RecurrenceRule(kind=ScheduleKind.DAILY),
    )

    store.materialize_due()
    job = store.get_job(job_id=created.job.id, owner_user_id=42)
    task = store.get_task(task_id=created.task.id, owner_user_id=42)

    assert job is not None
    assert job.status is ReminderJobStatus.NEEDS_REVIEW
    assert job.next_scheduled_for is None
    assert task is not None
    assert task.status is TaskStatus.NEEDS_REVIEW
    store.close()


def test_delivery_lease_retry_and_success_survive_restart(tmp_path):
    due = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    clock = MutableClock(due)
    database_path = tmp_path / "state.db"
    persona_id = setup_owner(database_path)
    store = SQLiteReminderStore(database_path, clock=clock)
    create_active(store, persona_id, local_start=due.replace(tzinfo=None))
    run = store.materialize_due()[0]

    first = store.lease_delivery()
    assert first is not None
    assert first.attempt == 1
    assert store.delivery_is_current(first) is True
    assert store.release_delivery(
        first,
        error_kind="telegram_unavailable",
        retry_delay_seconds=30,
    ) is True
    assert store.lease_delivery() is None

    clock.now += timedelta(seconds=30)
    second = store.lease_delivery()
    assert second is not None
    assert second.attempt == 2
    assert store.mark_delivery_sent(second, telegram_message_id=700) is True
    store.close()

    reopened = SQLiteReminderStore(database_path, clock=clock)
    persisted_run = reopened.list_runs(job_id=run.job_id, owner_user_id=42)[0]
    outbox = reopened.get_outbox(run_id=run.id, owner_user_id=42)
    assert persisted_run.status is ReminderRunStatus.DELIVERED
    assert outbox is not None
    assert outbox.status is OutboxStatus.SENT
    assert outbox.telegram_message_id == 700
    assert reopened.mark_delivery_sent(second, telegram_message_id=700) is True
    reopened.close()


def test_external_commit_after_reset_is_audited_without_restoring_reminder(tmp_path):
    due = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    clock = MutableClock(due)
    database_path = tmp_path / "state.db"
    persona_id = setup_owner(database_path)
    store = SQLiteReminderStore(database_path, clock=clock)
    create_active(store, persona_id, local_start=due.replace(tzinfo=None))
    store.materialize_due()
    lease = store.lease_delivery()
    assert lease is not None

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
    assert store.delivery_is_current(lease) is False
    store.purge_owner(owner_user_id=42, lifecycle_epoch=2)

    assert store.mark_delivery_sent(lease, telegram_message_id=700) is False
    assert store.mark_delivery_sent(lease, telegram_message_id=700) is False
    audit = store._connection.execute(
        """
        SELECT owner_user_id, lifecycle_epoch, event_type
        FROM reminder_delivery_audit
        """
    ).fetchall()
    assert [tuple(row) for row in audit] == [
        (42, 1, "external_commit_after_reset")
    ]
    assert store.list_tasks(owner_user_id=42) == ()
    store.close()
