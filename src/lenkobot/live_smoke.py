from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .aiogram_adapter import run_bot_delivery
from .action_confirmation import SQLiteActionConfirmationStore
from .application_service import TelegramApplicationService
from .memory import SQLiteMemoryStore
from .personas import PersonaCatalog
from .telegram_presentation import (
    TelegramResponse,
    TelegramResponseKind,
    TelegramResponsePort,
    confirmation_callback_data,
    parse_confirmation_callback_data,
)
from .telegram_router import (
    IncomingTelegramCallback,
    IncomingTelegramMessage,
    RoutedTurn,
    SQLiteConversationStore,
    TelegramRouter,
)


_COMMANDS = (
    "/start",
    "/help",
    "/persona",
    "/remember",
    "/memories",
    "/forget",
)


class LiveSmokeError(ValueError):
    pass


class LiveSmokeSettings(Protocol):
    data_root: Path
    allowed_user_id: int
    persona_catalog: PersonaCatalog


@dataclass(frozen=True, slots=True)
class LiveSmokeReport:
    commands: tuple[str, ...]

    @property
    def command_count(self) -> int:
        return len(self.commands)


async def run_live_smoke(
    settings: LiveSmokeSettings,
    bot_token: str,
    *,
    config_path: Path | str,
    confirmed: bool,
    delivery: Callable[
        [
            str,
            int,
            Callable[[TelegramResponsePort], Awaitable[LiveSmokeReport]],
        ],
        Awaitable[LiveSmokeReport],
    ] = run_bot_delivery,
    marker: str | None = None,
) -> LiveSmokeReport:
    if not confirmed:
        raise LiveSmokeError("live smoke requires explicit send confirmation")
    if not isinstance(bot_token, str) or not bot_token.strip():
        raise LiveSmokeError("Telegram bot token cannot be empty")

    data_root = _validate_data_root(settings.data_root, config_path=config_path)
    probe_text = _probe_text(marker)
    service, memory_store, conversation_store, confirmation_store = _open_scenario(
        data_root=data_root,
        allowed_user_id=settings.allowed_user_id,
        persona_catalog=settings.persona_catalog,
    )

    async def execute(response_port: TelegramResponsePort) -> LiveSmokeReport:
        return await _run_scenario(
            service=service,
            memory_store=memory_store,
            allowed_user_id=settings.allowed_user_id,
            persona_catalog=settings.persona_catalog,
            response_port=response_port,
            probe_text=probe_text,
        )

    try:
        return await delivery(bot_token, settings.allowed_user_id, execute)
    finally:
        memory_store.close()
        conversation_store.close()
        confirmation_store.close()


def _validate_data_root(data_root: Path | str, *, config_path: Path | str) -> Path:
    resolved_root = Path(data_root).resolve()
    config_directory = Path(config_path).resolve().parent
    if resolved_root == config_directory or config_directory in resolved_root.parents:
        raise LiveSmokeError("live smoke data root must be outside the config directory")
    if resolved_root.exists():
        raise LiveSmokeError("live smoke data root must not already exist")
    if not resolved_root.parent.is_dir():
        raise LiveSmokeError("live smoke data root parent must already exist")
    return resolved_root


def _probe_text(marker: str | None) -> str:
    selected = marker if marker is not None else uuid4().hex
    if not selected or len(selected) > 64 or any(
        not character.isascii()
        or (not character.isalnum() and character != "-")
        for character in selected
    ):
        raise LiveSmokeError("live smoke marker is invalid")
    return f"LenkoBot smoke {selected}"


def _open_scenario(
    *,
    data_root: Path,
    allowed_user_id: int,
    persona_catalog: PersonaCatalog,
) -> tuple[
    TelegramApplicationService,
    SQLiteMemoryStore,
    SQLiteConversationStore,
    SQLiteActionConfirmationStore,
]:
    data_root.mkdir()
    if data_root.resolve(strict=True) != data_root:
        raise LiveSmokeError("live smoke data root changed during creation")
    database_path = data_root / "state.db"
    conversation_store = SQLiteConversationStore(database_path)
    try:
        memory_store = SQLiteMemoryStore(database_path)
    except Exception:
        conversation_store.close()
        raise
    try:
        confirmation_store = SQLiteActionConfirmationStore(database_path)
    except Exception:
        conversation_store.close()
        memory_store.close()
        raise

    router = TelegramRouter(
        allowed_user_id,
        conversation_store,
        _DiscardingReplyPort(),
        persona_catalog,
    )
    service = TelegramApplicationService(
        router,
        persona_catalog,
        _ForbiddenProvider(),
        memory_store=memory_store,
        confirmation_store=confirmation_store,
    )
    return service, memory_store, conversation_store, confirmation_store


async def _run_scenario(
    *,
    service: TelegramApplicationService,
    memory_store: SQLiteMemoryStore,
    allowed_user_id: int,
    persona_catalog: PersonaCatalog,
    response_port: TelegramResponsePort,
    probe_text: str,
) -> LiveSmokeReport:
    def help_text(text: str) -> bool:
        return (
            "/remember <text>" in text
            and "/memories [page]" in text
            and "/forget [id]" in text
        )

    persona_text = "Выбери персону: " + ", ".join(
        persona.display_name for persona in persona_catalog.personas
    ) + "."

    await _run_command(
        service,
        allowed_user_id,
        "/start",
        response_port,
        help_text,
    )
    await _run_command(
        service,
        allowed_user_id,
        "/help",
        response_port,
        help_text,
    )
    await _run_command(
        service,
        allowed_user_id,
        "/persona",
        response_port,
        lambda text: text == persona_text,
    )
    await _run_command(
        service,
        allowed_user_id,
        f"/remember {probe_text}",
        response_port,
        lambda text: text == f"Запомнил: {probe_text}.",
    )

    records = memory_store.list_for_user(
        user_id=allowed_user_id,
        page=1,
        page_size=5,
    )
    matching_records = tuple(
        record for record in records if record.content == probe_text
    )
    if len(matching_records) != 1:
        raise LiveSmokeError("live smoke memory probe was not persisted once")
    memory_id = matching_records[0].id

    await _run_command(
        service,
        allowed_user_id,
        "/memories",
        response_port,
        lambda text: f"{memory_id}. [shared] {probe_text}" in text,
    )
    prompt = await _run_command(
        service,
        allowed_user_id,
        f"/forget {memory_id}",
        response_port,
        lambda text: text == f"Удалить запись {memory_id}: «{probe_text}»?",
    )
    token = _extract_confirmation_token(prompt)
    await _run_callback(
        service,
        allowed_user_id,
        confirmation_callback_data("confirm", token),
        response_port,
        lambda text: text == f"Удалено: запись {memory_id}.",
    )
    if memory_store.get(memory_id, user_id=allowed_user_id) is not None:
        raise LiveSmokeError("live smoke memory probe was not deleted")
    return LiveSmokeReport(commands=_COMMANDS)


def _extract_confirmation_token(response: TelegramResponse) -> str:
    for row in response.inline_keyboard:
        for button in row:
            parsed = parse_confirmation_callback_data(button.callback_data)
            if parsed is not None and parsed[0] == "confirm":
                return parsed[1]
    raise LiveSmokeError("live smoke confirmation prompt has no confirm button")


async def _run_callback(
    service: TelegramApplicationService,
    allowed_user_id: int,
    callback_data: str,
    response_port: TelegramResponsePort,
    expected_text: Callable[[str], bool],
) -> TelegramResponse:
    validating_port = _ValidatingResponsePort(
        response_port,
        target_chat_id=allowed_user_id,
        expected_text=expected_text,
    )
    await service.handle_callback(
        IncomingTelegramCallback(
            user_id=allowed_user_id,
            chat_id=allowed_user_id,
            chat_type="private",
            data=callback_data,
        ),
        validating_port,
    )
    return validating_port.require_response()


async def _run_command(
    service: TelegramApplicationService,
    allowed_user_id: int,
    command: str,
    response_port: TelegramResponsePort,
    expected_text: Callable[[str], bool],
) -> TelegramResponse:
    validating_port = _ValidatingResponsePort(
        response_port,
        target_chat_id=allowed_user_id,
        expected_text=expected_text,
    )
    await service.handle(
        IncomingTelegramMessage(
            user_id=allowed_user_id,
            chat_id=allowed_user_id,
            chat_type="private",
            text=command,
        ),
        validating_port,
    )
    return validating_port.require_response()


class _ValidatingResponsePort:
    def __init__(
        self,
        response_port: TelegramResponsePort,
        *,
        target_chat_id: int,
        expected_text: Callable[[str], bool],
    ) -> None:
        self._response_port = response_port
        self._target_chat_id = target_chat_id
        self._expected_text = expected_text
        self._response: TelegramResponse | None = None

    async def send(self, response: TelegramResponse) -> None:
        if self._response is not None:
            raise LiveSmokeError("live smoke command produced multiple responses")
        if (
            response.chat_id != self._target_chat_id
            or response.kind is not TelegramResponseKind.FINAL
            or not self._expected_text(response.text)
        ):
            raise LiveSmokeError("live smoke command produced an unexpected response")
        self._response = response
        await self._response_port.send(response)

    def require_response(self) -> TelegramResponse:
        if self._response is None:
            raise LiveSmokeError("live smoke command produced no response")
        return self._response


class _ForbiddenProvider:
    def respond(self, prompt: str) -> None:
        raise LiveSmokeError("live smoke attempted to invoke the provider")


class _DiscardingReplyPort:
    def send(self, turn: RoutedTurn) -> None:
        return None
