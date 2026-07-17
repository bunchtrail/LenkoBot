from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class TelegramResponseKind(StrEnum):
    STATUS = "status"
    NOTICE = "notice"
    FINAL = "final"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TelegramResponse:
    chat_id: int
    kind: TelegramResponseKind
    text: str


class TelegramResponsePort(Protocol):
    async def send(self, response: TelegramResponse) -> None: ...


@dataclass(frozen=True, slots=True)
class TelegramCommand:
    name: str
    arguments: tuple[str, ...]


def parse_telegram_command(text: str) -> TelegramCommand | None:
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None

    command_token = parts[0][1:]
    if not command_token:
        return None

    command_name, separator, bot_name = command_token.partition("@")
    if separator and not bot_name:
        return None
    if not command_name:
        return None

    return TelegramCommand(
        name=command_name.casefold(),
        arguments=tuple(parts[1:]),
    )
