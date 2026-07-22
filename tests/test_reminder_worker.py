import asyncio
from datetime import datetime, timedelta, timezone

from lenkobot.action_confirmation import (
    ActionConfirmationService,
    SQLiteActionConfirmationStore,
)
from lenkobot.reminder_schedule import RecurrenceRule, ScheduleKind
from lenkobot.reminder_store import (
    OutboxStatus,
    ReminderDraft,
    ReminderRunStatus,
    SQLiteReminderStore,
)
from lenkobot.reminder_worker import ReminderDeliveryWorker, ReminderScheduler
from lenkobot.sqlite_schema import open_state_database
from lenkobot.telegram_presentation import TelegramSentMessage


class MutableClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class RecordingPort:
    def __init__(self, *, error=None, before_return=None):
        self.error = error
        self.before_return = before_return
        self.responses = []

    async def send(self, response):
        self.responses.append(response)
        if self.error is not None:
            raise self.error
        if self.before_return is not None:
            self.before_return()
        return TelegramSentMessage(chat_id=response.chat_id, message_id=700)


def setup_active(database_path, clock):
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
    store = SQLiteReminderStore(database_path, clock=clock)
    created = store.create_draft(
        ReminderDraft(
            owner_user_id=42,
            persona_id=persona_id,
            chat_id=500,
            text="Принять лекарство",
            local_start=clock().replace(tzinfo=None),
            timezone_name="UTC",
            recurrence=RecurrenceRule(kind=ScheduleKind.ONCE),
        )
    )
    store.mark_awaiting_confirmation(task_id=created.task.id, owner_user_id=42)
    store.activate(task_id=created.task.id, owner_user_id=42)
    return store, created


def build_worker(database_path, store, clock):
    confirmation_store = SQLiteActionConfirmationStore(database_path, clock=clock)
    worker = ReminderDeliveryWorker(
        store,
        ActionConfirmationService(confirmation_store),
        retry_delay_seconds=30,
    )
    return worker, confirmation_store


def test_scheduler_and_worker_deliver_once_with_durable_action_buttons(tmp_path):
    now = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    clock = MutableClock(now)
    database_path = tmp_path / "state.db"
    store, created = setup_active(database_path, clock)
    worker, confirmation_store = build_worker(database_path, store, clock)
    scheduler = ReminderScheduler(store)
    port = RecordingPort()

    assert len(scheduler.run_once()) == 1
    assert asyncio.run(worker.run_once(port)) is True
    assert asyncio.run(worker.run_once(port)) is False

    assert len(port.responses) == 1
    response = port.responses[0]
    assert response.text == "напоминание: Принять лекарство"
    assert [button.text for row in response.inline_keyboard for button in row] == [
        "Через 10 минут",
        "Готово",
        "Отменить",
    ]
    actions = confirmation_store._connection.execute(
        "SELECT action_type FROM action_confirmation ORDER BY action_type"
    ).fetchall()
    assert [row[0] for row in actions] == [
        "cancel_reminder",
        "complete_reminder",
        "snooze_reminder",
    ]
    runs = store.list_runs(job_id=created.job.id, owner_user_id=42)
    assert runs[0].status is ReminderRunStatus.DELIVERED
    outbox = store.get_outbox(run_id=runs[0].id, owner_user_id=42)
    assert outbox.status is OutboxStatus.SENT
    assert outbox.telegram_message_id == 700


def test_worker_releases_failed_delivery_for_bounded_retry(tmp_path):
    now = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    clock = MutableClock(now)
    database_path = tmp_path / "state.db"
    store, created = setup_active(database_path, clock)
    worker, _ = build_worker(database_path, store, clock)
    ReminderScheduler(store).run_once()

    assert asyncio.run(worker.run_once(RecordingPort(error=RuntimeError("secret")))) is True
    run = store.list_runs(job_id=created.job.id, owner_user_id=42)[0]
    outbox = store.get_outbox(run_id=run.id, owner_user_id=42)
    assert run.status is ReminderRunStatus.DUE
    assert outbox.status is OutboxStatus.PENDING
    assert outbox.attempt == 1
    assert outbox.available_at == now + timedelta(seconds=30)


def test_worker_quiesce_blocks_old_epoch_before_send(tmp_path):
    now = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    clock = MutableClock(now)
    database_path = tmp_path / "state.db"
    store, _ = setup_active(database_path, clock)
    worker, _ = build_worker(database_path, store, clock)
    ReminderScheduler(store).run_once()
    worker.quiesce(42, 1)
    port = RecordingPort()

    assert asyncio.run(worker.run_once(port)) is True
    assert port.responses == []


def test_worker_audits_external_commit_when_reset_wins_before_persistence(tmp_path):
    now = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    clock = MutableClock(now)
    database_path = tmp_path / "state.db"
    store, _ = setup_active(database_path, clock)
    worker, _ = build_worker(database_path, store, clock)
    ReminderScheduler(store).run_once()

    def reset_owner():
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

    port = RecordingPort(before_return=reset_owner)
    assert asyncio.run(worker.run_once(port)) is True

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
