from dataclasses import dataclass
from enum import StrEnum
from html import escape
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit


_PERSONA_CALLBACK_PREFIX = "persona:v1:"
_CONFIRM_CALLBACK_PREFIX = "confirm:v1:"
_CANCEL_CALLBACK_PREFIX = "cancel:v1:"
_MEMORIES_PAGE_CALLBACK_PREFIX = "mem:v1:"
_FORGET_CALLBACK_PREFIX = "forget:v1:"
_TELEGRAM_CALLBACK_DATA_LIMIT = 64
TELEGRAM_MESSAGE_TEXT_LIMIT = 4096
_SOURCE_COUNT_LIMIT = 5
_SOURCE_TITLE_LIMIT = 96
_SOURCE_URL_LIMIT = 512


@dataclass(frozen=True, slots=True)
class TelegramCommandDefinition:
    command: str
    usage: str
    description: str


TELEGRAM_COMMANDS = (
    TelegramCommandDefinition("start", "/start", "открыть управление"),
    TelegramCommandDefinition("help", "/help", "показать команды"),
    TelegramCommandDefinition("persona", "/persona", "выбрать персону"),
    TelegramCommandDefinition("new", "/new", "закрыть текущий разговор"),
    TelegramCommandDefinition(
        "remind",
        "/remind <что и когда>",
        "создать напоминание",
    ),
    TelegramCommandDefinition("tasks", "/tasks", "показать задачи"),
    TelegramCommandDefinition(
        "timezone",
        "/timezone [IANA timezone]",
        "настроить часовой пояс",
    ),
    TelegramCommandDefinition(
        "quiet",
        "/quiet [HH:MM-HH:MM|off]",
        "настроить тихие часы",
    ),
    TelegramCommandDefinition(
        "remember",
        "/remember <text>",
        "сохранить общую запись",
    ),
    TelegramCommandDefinition(
        "memories",
        "/memories [page]",
        "показать записи памяти",
    ),
    TelegramCommandDefinition("forget", "/forget [id]", "удалить запись памяти"),
)


def render_command_index() -> str:
    lines = ["Доступные команды:"]
    lines.extend(
        f"{item.usage} — {item.description}." for item in TELEGRAM_COMMANDS
    )
    return "\n".join(lines)


class TelegramResponseKind(StrEnum):
    STATUS = "status"
    NOTICE = "notice"
    FINAL = "final"
    ERROR = "error"


class TelegramParseMode(StrEnum):
    HTML = "HTML"


@dataclass(frozen=True, slots=True)
class TelegramResponse:
    chat_id: int
    kind: TelegramResponseKind
    text: str
    inline_keyboard: tuple[tuple["TelegramInlineButton", ...], ...] = ()
    parse_mode: TelegramParseMode | None = None


@dataclass(frozen=True, slots=True)
class TelegramInlineButton:
    text: str
    callback_data: str


@dataclass(frozen=True, slots=True)
class TelegramSentMessage:
    chat_id: int
    message_id: int


@dataclass(frozen=True, slots=True)
class TelegramWebSource:
    title: str
    url: str


class TelegramResponsePort(Protocol):
    async def send(
        self,
        response: TelegramResponse,
    ) -> TelegramSentMessage | None: ...


@runtime_checkable
class TelegramEditableResponsePort(Protocol):
    async def edit(
        self,
        handle: TelegramSentMessage,
        response: TelegramResponse,
    ) -> bool: ...

    def bound_handle(self) -> TelegramSentMessage | None: ...


@dataclass(frozen=True, slots=True)
class TelegramCommand:
    name: str
    arguments: tuple[str, ...]


def persona_callback_data(persona_key: str) -> str:
    if not isinstance(persona_key, str) or not persona_key:
        raise ValueError("persona key cannot be empty")
    data = f"{_PERSONA_CALLBACK_PREFIX}{persona_key}"
    if len(data.encode("utf-8")) > _TELEGRAM_CALLBACK_DATA_LIMIT:
        raise ValueError("persona callback data exceeds Telegram limit")
    return data


def parse_persona_callback_data(data: str) -> str | None:
    if not isinstance(data, str) or not data.startswith(_PERSONA_CALLBACK_PREFIX):
        return None
    if len(data.encode("utf-8")) > _TELEGRAM_CALLBACK_DATA_LIMIT:
        return None
    persona_key = data.removeprefix(_PERSONA_CALLBACK_PREFIX)
    return persona_key or None


def confirmation_callback_data(action: str, token: str) -> str:
    if action == "confirm":
        prefix = _CONFIRM_CALLBACK_PREFIX
    elif action == "cancel":
        prefix = _CANCEL_CALLBACK_PREFIX
    else:
        raise ValueError("confirmation action must be confirm or cancel")
    if not isinstance(token, str) or not token:
        raise ValueError("confirmation token cannot be empty")
    data = f"{prefix}{token}"
    if len(data.encode("utf-8")) > _TELEGRAM_CALLBACK_DATA_LIMIT:
        raise ValueError("confirmation callback data exceeds Telegram limit")
    return data


def parse_confirmation_callback_data(data: str) -> tuple[str, str] | None:
    if not isinstance(data, str):
        return None
    if len(data.encode("utf-8")) > _TELEGRAM_CALLBACK_DATA_LIMIT:
        return None
    if data.startswith(_CONFIRM_CALLBACK_PREFIX):
        token = data.removeprefix(_CONFIRM_CALLBACK_PREFIX)
        return ("confirm", token) if token else None
    if data.startswith(_CANCEL_CALLBACK_PREFIX):
        token = data.removeprefix(_CANCEL_CALLBACK_PREFIX)
        return ("cancel", token) if token else None
    return None


def memories_page_callback_data(page: int) -> str:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("memories page must be a positive integer")
    return f"{_MEMORIES_PAGE_CALLBACK_PREFIX}{page}"


def parse_memories_page_callback_data(data: str) -> int | None:
    if not isinstance(data, str) or not data.startswith(_MEMORIES_PAGE_CALLBACK_PREFIX):
        return None
    raw_page = data.removeprefix(_MEMORIES_PAGE_CALLBACK_PREFIX)
    if not raw_page.isdigit():
        return None
    page = int(raw_page)
    return page if page >= 1 else None


def forget_callback_data(memory_id: int) -> str:
    if isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id < 1:
        raise ValueError("memory id must be a positive integer")
    return f"{_FORGET_CALLBACK_PREFIX}{memory_id}"


def parse_forget_callback_data(data: str) -> int | None:
    if not isinstance(data, str) or not data.startswith(_FORGET_CALLBACK_PREFIX):
        return None
    raw_id = data.removeprefix(_FORGET_CALLBACK_PREFIX)
    if not raw_id.isdigit():
        return None
    memory_id = int(raw_id)
    return memory_id if memory_id >= 1 else None


def split_telegram_text(
    text: str,
    *,
    limit: int = TELEGRAM_MESSAGE_TEXT_LIMIT,
) -> tuple[str, ...]:
    if limit < 1:
        raise ValueError("split limit must be positive")
    chunks = []
    remaining = text
    while len(remaining) > limit:
        boundary = _split_boundary(remaining, limit)
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:].lstrip("\n").lstrip(" ")
        if not remaining:
            break
    if remaining or not chunks:
        chunks.append(remaining)
    return tuple(chunks)


def render_sources_html(
    sources: tuple[TelegramWebSource, ...],
) -> str:
    lines = ["<b>Источники:</b>"]
    seen_urls = set()
    for source in sources:
        if len(lines) > _SOURCE_COUNT_LIMIT:
            break
        if not isinstance(source, TelegramWebSource):
            continue
        title = " ".join(source.title.split())
        url = source.url.strip()
        if not title or url in seen_urls or not _is_http_url(url):
            continue
        if len(url) > _SOURCE_URL_LIMIT:
            continue
        if len(title) > _SOURCE_TITLE_LIMIT:
            title = title[: _SOURCE_TITLE_LIMIT - 1].rstrip() + "…"
        line = (
            f'{len(lines)}. <a href="{escape(url, quote=True)}">'
            f"{escape(title, quote=True)}</a>"
        )
        candidate = "\n".join((*lines, line))
        if len(candidate) > TELEGRAM_MESSAGE_TEXT_LIMIT:
            break
        lines.append(line)
        seen_urls.add(url)
    return "\n".join(lines) if len(lines) > 1 else ""


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _split_boundary(text: str, limit: int) -> int:
    window = text[:limit]
    paragraph = window.rfind("\n\n")
    if paragraph > 0:
        return paragraph
    line = window.rfind("\n")
    if line > 0:
        return line
    space = window.rfind(" ")
    if space > 0:
        return space
    return limit


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
