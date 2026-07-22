import asyncio
from datetime import datetime, timedelta, timezone

from lenkobot.action_confirmation import (
    ActionConfirmationService,
    SQLiteActionConfirmationStore,
)
from lenkobot.application_service import TelegramApplicationService
from lenkobot.personas import PersonaCatalog
from lenkobot.reminder_parser import ReminderParser
from lenkobot.reminder_store import (
    ReminderJobStatus,
    SQLiteReminderStore,
    TaskStatus,
)
from lenkobot.telegram_presentation import parse_confirmation_callback_data
from lenkobot.telegram_router import (
    IncomingTelegramCallback,
    IncomingTelegramMessage,
    SQLiteConversationStore,
    TelegramRouter,
)


_NOW = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)


class RecordingResponsePort:
    def __init__(self):
        self.responses = []

    async def send(self, response):
        self.responses.append(response)


class RejectingTextProvider:
    def __init__(self):
        self.prompts = []

    def respond(self, prompt):
        self.prompts.append(prompt)
        raise AssertionError("ordinary text provider must not handle reminders")


class StructuredReminderProvider:
    def __init__(self, payload=None, error=None):
        self.payload = payload or reminder_payload()
        self.error = error
        self.calls = []

    def respond(self, prompt, *, schema_name, schema):
        self.calls.append((prompt, schema_name, schema))
        if self.error is not None:
            raise self.error
        return self.payload


def reminder_payload(**overrides):
    payload = {
        "text": "Позвонить маме",
        "local_start": "2026-07-22T18:00:00",
        "timezone_name": None,
        "kind": "once",
        "interval": 1,
        "weekdays": [],
        "monthday": None,
        "count": None,
        "until_local": None,
        "urgent": False,
    }
    payload.update(overrides)
    return payload


def build_service(tmp_path, *, structured_provider=None):
    config_path = tmp_path / "personas.toml"
    config_path.write_text(
        """
default_persona_key = "companion"

[[personas]]
key = "companion"
display_name = "Companion"
identity_prompt = "A calm companion."
identity_version = 1
""".strip(),
        encoding="utf-8",
    )
    catalog = PersonaCatalog.from_toml(config_path)
    database_path = tmp_path / "state.db"
    conversation_store = SQLiteConversationStore(database_path)
    router = TelegramRouter(
        allowed_user_id=42,
        store=conversation_store,
        reply_port=RecordingResponsePort(),
        persona_catalog=catalog,
    )
    reminder_store = SQLiteReminderStore(database_path, clock=lambda: _NOW)
    confirmation_store = SQLiteActionConfirmationStore(
        database_path,
        clock=lambda: _NOW,
    )
    confirmation_service = ActionConfirmationService(confirmation_store)
    parser_provider = structured_provider or StructuredReminderProvider()
    response_port = RecordingResponsePort()
    text_provider = RejectingTextProvider()
    service = TelegramApplicationService(
        router=router,
        persona_catalog=catalog,
        provider=text_provider,
        response_port=response_port,
        confirmation_service=confirmation_service,
        reminder_store=reminder_store,
        reminder_parser=ReminderParser(parser_provider, clock=lambda: _NOW),
    )
    return (
        service,
        reminder_store,
        confirmation_store,
        response_port,
        text_provider,
        parser_provider,
    )


def message(text, *, user_id=42, chat_id=500, chat_type="private"):
    return IncomingTelegramMessage(
        user_id=user_id,
        chat_id=chat_id,
        chat_type=chat_type,
        text=text,
    )


def callback(data, *, user_id=42, chat_id=500, chat_type="private"):
    return IncomingTelegramCallback(
        user_id=user_id,
        chat_id=chat_id,
        chat_type=chat_type,
        data=data,
    )


def button_data(response, text):
    for row in response.inline_keyboard:
        for button in row:
            if button.text == text:
                return button.callback_data
    raise AssertionError(f"button {text!r} was not rendered")


def test_explicit_text_creates_draft_and_confirm_activates_without_chat_provider(
    tmp_path,
):
    service, store, _, responses, text_provider, parser_provider = build_service(
        tmp_path
    )

    asyncio.run(service.handle(message("напомни позвонить маме завтра в шесть")))

    tasks = store.list_tasks(owner_user_id=42)
    assert len(tasks) == 1
    assert tasks[0].status is TaskStatus.AWAITING_CONFIRMATION
    job = store.get_job_for_task(task_id=tasks[0].id, owner_user_id=42)
    assert job is not None
    assert job.status is ReminderJobStatus.AWAITING_CONFIRMATION
    assert job.next_scheduled_for is None
    assert text_provider.prompts == []
    assert len(parser_provider.calls) == 1
    prompt = responses.responses[-1]
    assert "22.07.2026 18:00" in prompt.text
    assert "UTC" in prompt.text

    confirm_data = button_data(prompt, "Создать")
    asyncio.run(service.handle_callback(callback(confirm_data)))

    active = store.get_job_for_task(task_id=tasks[0].id, owner_user_id=42)
    assert active is not None
    assert active.status is ReminderJobStatus.ACTIVE
    assert active.next_scheduled_for == datetime(
        2026,
        7,
        22,
        18,
        0,
        tzinfo=timezone.utc,
    )
    assert "создано" in responses.responses[-1].text.casefold()


def test_cancelled_reminder_confirmation_is_durable_and_never_activates(tmp_path):
    service, store, confirmation_store, responses, _, _ = build_service(tmp_path)
    asyncio.run(service.handle(message("/remind позвонить маме завтра")))
    task = store.list_tasks(owner_user_id=42)[0]

    asyncio.run(
        service.handle_callback(
            callback(button_data(responses.responses[-1], "Отмена"))
        )
    )

    assert store.get_task(task_id=task.id, owner_user_id=42).status is TaskStatus.CANCELLED
    outcome = confirmation_store._connection.execute(
        "SELECT outcome FROM action_confirmation ORDER BY created_at DESC LIMIT 1"
    ).fetchone()[0]
    assert outcome == "cancelled"
    assert store.materialize_due() == ()


def test_profile_timezone_and_quiet_hours_become_draft_defaults(tmp_path):
    service, store, _, responses, _, _ = build_service(tmp_path)

    asyncio.run(service.handle(message("/timezone Europe/Moscow")))
    asyncio.run(service.handle(message("/quiet 23:00-07:00")))
    asyncio.run(service.handle(message("/remind позвонить маме завтра")))

    task = store.list_tasks(owner_user_id=42)[0]
    job = store.get_job_for_task(task_id=task.id, owner_user_id=42)
    assert job is not None
    assert job.timezone_name == "Europe/Moscow"
    assert job.quiet_start_minute == 23 * 60
    assert job.quiet_end_minute == 7 * 60
    assert "Europe/Moscow" in responses.responses[-1].text

    asyncio.run(service.handle(message("/quiet off")))
    policy = store.get_profile_policy(owner_user_id=42)
    assert policy.quiet_start_minute is None
    assert policy.quiet_end_minute is None


def test_tasks_buttons_complete_and_snooze_through_durable_receipts(tmp_path):
    service, store, _, responses, _, _ = build_service(tmp_path)
    asyncio.run(service.handle(message("/remind позвонить маме завтра")))
    asyncio.run(
        service.handle_callback(
            callback(button_data(responses.responses[-1], "Создать"))
        )
    )
    task = store.list_tasks(owner_user_id=42)[0]

    asyncio.run(service.handle(message("/tasks")))
    listing = responses.responses[-1]
    assert "Позвонить маме" in listing.text
    snooze_data = button_data(listing, "Через 10 минут")
    parsed = parse_confirmation_callback_data(snooze_data)
    assert parsed is not None and parsed[0] == "confirm"
    asyncio.run(service.handle_callback(callback(snooze_data)))
    job = store.get_job_for_task(task_id=task.id, owner_user_id=42)
    runs = store.list_runs(job_id=job.id, owner_user_id=42)
    assert len(runs) == 1
    assert runs[0].scheduled_for == _NOW + timedelta(minutes=10)

    asyncio.run(service.handle(message("/tasks")))
    complete_data = button_data(responses.responses[-1], "Готово")
    asyncio.run(service.handle_callback(callback(complete_data)))
    assert store.get_task(task_id=task.id, owner_user_id=42).status is TaskStatus.COMPLETED


def test_reminder_parser_is_not_called_before_owner_private_authorization(tmp_path):
    service, store, _, _, _, parser_provider = build_service(tmp_path)

    asyncio.run(
        service.handle(
            message(
                "напомни украсть данные",
                user_id=99,
            )
        )
    )
    asyncio.run(
        service.handle(
            message(
                "напомни украсть данные",
                chat_type="group",
            )
        )
    )

    assert parser_provider.calls == []
    assert store.list_tasks(owner_user_id=42) == ()
