from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from typing import Protocol
from uuid import uuid4

from .personas import PersonaCatalog
from .telegram_e2e_credentials import TelegramE2ECredentialState
from .telegram_presentation import render_command_index


_BOT_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{5,32}$")
class TelegramE2EError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramE2ESettings:
    allowed_user_id: int
    bot_user_id: int
    bot_username: str
    persona_display_names: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class TelegramE2EMessage:
    id: int
    text: str


@dataclass(frozen=True, slots=True)
class TelegramE2EStep:
    command: str
    response_text: str


@dataclass(frozen=True, slots=True)
class TelegramE2EReport:
    steps: tuple[TelegramE2EStep, ...]

    @property
    def command_count(self) -> int:
        return len(self.steps)


class TelegramE2ETransport(Protocol):
    async def exchange(self, command: str) -> TelegramE2EMessage: ...

    async def click_button(
        self,
        message_id: int,
        *,
        button_text: str,
    ) -> TelegramE2EMessage: ...

    async def close(self) -> None: ...


def load_telegram_e2e_settings(config_path: Path | str) -> TelegramE2ESettings:
    path = Path(config_path)
    with path.open("rb") as config_file:
        data = tomllib.load(config_file)
    telegram = data.get("telegram")
    e2e = data.get("telegram_e2e")
    if not isinstance(telegram, dict) or not isinstance(e2e, dict):
        raise ValueError(
            "Telegram E2E configuration must contain telegram and telegram_e2e tables"
        )

    allowed_user_id = telegram.get("allowed_user_id")
    bot_user_id = e2e.get("bot_user_id")
    bot_username = e2e.get("bot_username")
    for value, label in (
        (allowed_user_id, "allowed_user_id"),
        (bot_user_id, "bot_user_id"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"Telegram E2E {label} must be a positive integer")
    if allowed_user_id == bot_user_id:
        raise ValueError("Telegram E2E user and bot IDs must differ")
    if (
        not isinstance(bot_username, str)
        or not _BOT_USERNAME_PATTERN.fullmatch(bot_username)
        or not bot_username.casefold().endswith("bot")
    ):
        raise ValueError("Telegram E2E bot_username is invalid")
    try:
        catalog = PersonaCatalog.from_toml(path)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("persona configuration is invalid") from error
    return TelegramE2ESettings(
        allowed_user_id=allowed_user_id,
        bot_user_id=bot_user_id,
        bot_username=bot_username,
        persona_display_names=tuple(
            (persona.key, persona.display_name) for persona in catalog.personas
        ),
    )


def validate_telegram_e2e_bot_data_root(
    data_root: Path | str,
    *,
    config_path: Path | str,
) -> Path:
    resolved_root = Path(data_root).resolve()
    config_directory = Path(config_path).resolve().parent
    if resolved_root == config_directory or config_directory in resolved_root.parents:
        raise ValueError("Telegram E2E bot data root must be outside config directory")
    if resolved_root.exists():
        raise ValueError("Telegram E2E bot data root must not already exist")
    if not resolved_root.parent.is_dir():
        raise ValueError("Telegram E2E bot data root parent must already exist")
    return resolved_root


def prepare_telegram_e2e_bot_data_root(
    data_root: Path | str,
    *,
    config_path: Path | str,
) -> Path:
    resolved_root = validate_telegram_e2e_bot_data_root(
        data_root,
        config_path=config_path,
    )
    resolved_root.mkdir()
    if resolved_root.resolve(strict=True) != resolved_root:
        raise ValueError("Telegram E2E bot data root changed during creation")
    return resolved_root


async def run_telegram_e2e(
    settings: TelegramE2ESettings,
    credentials: TelegramE2ECredentialState,
    *,
    confirmed: bool,
    transport_factory: Callable[
        [TelegramE2ESettings, TelegramE2ECredentialState],
        Awaitable[TelegramE2ETransport],
    ],
    marker: str | None = None,
) -> TelegramE2EReport:
    if not confirmed:
        raise TelegramE2EError("Telegram E2E requires explicit send confirmation")
    if credentials.user_id != settings.allowed_user_id:
        raise TelegramE2EError(
            "Telegram E2E credential user does not match configured allowlist"
        )
    probe = _probe_text(marker)
    try:
        transport = await transport_factory(settings, credentials)
    except TelegramE2EError:
        raise
    except Exception:
        raise TelegramE2EError("Telegram E2E transport could not start") from None

    try:
        return await _run_scenario(settings, transport, probe)
    finally:
        await transport.close()


async def _run_scenario(
    settings: TelegramE2ESettings,
    transport: TelegramE2ETransport,
    probe: str,
) -> TelegramE2EReport:
    steps = []
    last_message_id = 0

    index_text = render_command_index()

    message = await _checked_exchange(
        transport,
        "/start",
        last_message_id=last_message_id,
        expected=lambda text: text.endswith(index_text) and text != index_text,
    )
    last_message_id = message.id
    steps.append(TelegramE2EStep(command="/start", response_text=message.text))

    message = await _checked_exchange(
        transport,
        "/help",
        last_message_id=last_message_id,
        expected=lambda text: text == index_text,
    )
    last_message_id = message.id
    steps.append(TelegramE2EStep(command="/help", response_text=index_text))

    persona_text = "Выбери персону: " + ", ".join(
        display_name for _, display_name in settings.persona_display_names
    ) + "."

    message = await _checked_exchange(
        transport,
        "/persona",
        last_message_id=last_message_id,
        expected=lambda text: text == persona_text,
    )
    last_message_id = message.id
    steps.append(TelegramE2EStep(command="/persona", response_text=message.text))

    message = await _checked_exchange(
        transport,
        f"/remember {probe}",
        last_message_id=last_message_id,
        expected=lambda text: text == f"Запомнил: {probe}.",
    )
    last_message_id = message.id
    steps.append(
        TelegramE2EStep(command="/remember", response_text="Запомнил: <probe>.")
    )

    message = await _checked_exchange(
        transport,
        "/memories",
        last_message_id=last_message_id,
        expected=lambda text: probe in text,
    )
    last_message_id = message.id
    memory_lines = message.text.splitlines()
    memory_match = (
        None
        if len(memory_lines) != 2
        or re.fullmatch(r"Память, страница 1 из \d+:", memory_lines[0]) is None
        else re.fullmatch(
            rf"(\d+)\. \[shared\] {re.escape(probe)}",
            memory_lines[1],
        )
    )
    if memory_match is None:
        raise TelegramE2EError("Telegram E2E memory reply is invalid")
    memory_id = int(memory_match.group(1))
    normalized_memories = f"{memory_lines[0]}\n<id>. [shared] <probe>"
    steps.append(
        TelegramE2EStep(command="/memories", response_text=normalized_memories)
    )

    prompt = await _checked_exchange(
        transport,
        f"/forget {memory_id}",
        last_message_id=last_message_id,
        expected=lambda text: text.startswith(f"Удалить запись {memory_id}: «")
        and probe in text,
    )
    await _checked_click(
        transport,
        prompt,
        button_text="Удалить",
        expected=lambda text: text == f"Удалено: запись {memory_id}.",
    )
    steps.append(
        TelegramE2EStep(
            command="/forget",
            response_text="Удалено: запись <id>.",
        )
    )
    return TelegramE2EReport(steps=tuple(steps))


async def _checked_click(
    transport: TelegramE2ETransport,
    prompt: TelegramE2EMessage,
    *,
    button_text: str,
    expected: Callable[[str], bool],
) -> TelegramE2EMessage:
    try:
        message = await transport.click_button(prompt.id, button_text=button_text)
    except TelegramE2EError:
        raise
    except Exception:
        raise TelegramE2EError("Telegram E2E button click failed") from None
    if (
        not isinstance(message, TelegramE2EMessage)
        or message.id != prompt.id
        or not isinstance(message.text, str)
        or not expected(message.text)
    ):
        raise TelegramE2EError("Telegram E2E received an unexpected confirmation")
    return message


async def _checked_exchange(
    transport: TelegramE2ETransport,
    command: str,
    *,
    last_message_id: int,
    expected: Callable[[str], bool],
) -> TelegramE2EMessage:
    try:
        message = await transport.exchange(command)
    except TelegramE2EError:
        raise
    except Exception:
        raise TelegramE2EError("Telegram E2E command exchange failed") from None
    if (
        not isinstance(message, TelegramE2EMessage)
        or message.id <= last_message_id
        or not isinstance(message.text, str)
        or not expected(message.text)
    ):
        raise TelegramE2EError("Telegram E2E received an unexpected reply")
    return message


def _probe_text(marker: str | None) -> str:
    selected = marker if marker is not None else uuid4().hex
    if not selected or len(selected) > 64 or any(
        not character.isascii()
        or (not character.isalnum() and character != "-")
        for character in selected
    ):
        raise TelegramE2EError("Telegram E2E marker is invalid")
    return f"LenkoBot E2E {selected}"
