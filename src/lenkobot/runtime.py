import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import tomllib

from .aiogram_adapter import AiogramTelegramResponsePort, run_polling
from .application_service import TelegramApplicationService
from .context_builder import ContextBuilder
from .memory import SQLiteMemoryStore
from .oauth_credentials import (
    WindowsOAuthCredentialStore,
    WindowsOAuthRefreshMutex,
    XaiOAuthDeviceClient,
)
from .personas import PersonaCatalog
from .telegram_router import RoutedTurn, SQLiteConversationStore, TelegramRouter
from .xai_provider import (
    CredentialPolicy,
    CredentialUnavailable,
    OAuthCredentialSource,
    OAuthRefreshCoordinator,
    ProviderRequestError,
    XaiOAuthRefreshClient,
    XaiProvider,
    XaiResponsesTransport,
)


_INFERENCE_BASE_URL = "https://api.x.ai/v1"
_MODEL = "grok-4.5"
_HERMES_REFERENCE_XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    data_root: Path
    allowed_user_id: int
    oauth_client_id: str
    persona_catalog: PersonaCatalog


class _DiscardingReplyPort:
    def send(self, turn: RoutedTurn) -> None:
        return None


def load_runtime_settings(
    config_path: Path | str,
    *,
    data_root: Path | str | None = None,
) -> RuntimeSettings:
    path = Path(config_path)
    with path.open("rb") as config_file:
        data = tomllib.load(config_file)

    telegram = data.get("telegram")
    oauth = data.get("oauth")
    if not isinstance(telegram, dict):
        raise ValueError("runtime configuration must contain a telegram table")
    if oauth is not None and not isinstance(oauth, dict):
        raise ValueError("runtime configuration oauth value must be a table")

    allowed_user_id = telegram.get("allowed_user_id")
    if (
        isinstance(allowed_user_id, bool)
        or not isinstance(allowed_user_id, int)
        or allowed_user_id <= 0
    ):
        raise ValueError("Telegram allowed_user_id must be a positive integer")

    client_id = (
        oauth.get("client_id", _HERMES_REFERENCE_XAI_OAUTH_CLIENT_ID)
        if isinstance(oauth, dict)
        else _HERMES_REFERENCE_XAI_OAUTH_CLIENT_ID
    )
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("OAuth client_id cannot be empty")

    try:
        persona_catalog = PersonaCatalog.from_toml(path)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("persona configuration is invalid") from error

    selected_data_root = Path(data_root) if data_root is not None else path.parent / "data"
    return RuntimeSettings(
        data_root=selected_data_root,
        allowed_user_id=allowed_user_id,
        oauth_client_id=client_id,
        persona_catalog=persona_catalog,
    )


def login(
    settings: RuntimeSettings,
    *,
    output: Callable[[str], None] = print,
) -> None:
    store = WindowsOAuthCredentialStore()
    lock = WindowsOAuthRefreshMutex(store.target_name)
    client = XaiOAuthDeviceClient(client_id=settings.oauth_client_id)
    authorization = client.start_device_authorization()
    output(f"Open: {authorization.verification_uri}")
    output(f"Code: {authorization.user_code}")
    client.complete_device_authorization(authorization, store=store, lock=lock)
    output("OAuth login completed.")


async def run_application(
    settings: RuntimeSettings,
    bot_token: str,
    *,
    polling: Callable[..., Awaitable[None]] = run_polling,
) -> None:
    if not isinstance(bot_token, str) or not bot_token.strip():
        raise ValueError("Telegram bot token cannot be empty")

    store = WindowsOAuthCredentialStore()
    if store.load() is None:
        raise CredentialUnavailable("OAuth credential state is unavailable")

    lock = WindowsOAuthRefreshMutex(store.target_name)
    coordinator = OAuthRefreshCoordinator(
        store,
        XaiOAuthRefreshClient(client_id=settings.oauth_client_id),
        lock=lock,
    )
    provider = XaiProvider(
        XaiResponsesTransport(),
        CredentialPolicy.OAUTH_ONLY,
        oauth_source=OAuthCredentialSource(coordinator, base_url=_INFERENCE_BASE_URL),
        model=_MODEL,
    )

    settings.data_root.mkdir(parents=True, exist_ok=True)
    database_path = settings.data_root / "state.db"
    conversation_store = SQLiteConversationStore(database_path)
    try:
        memory_store = SQLiteMemoryStore(database_path)
    except Exception:
        conversation_store.close()
        raise

    router = TelegramRouter(
        settings.allowed_user_id,
        conversation_store,
        _DiscardingReplyPort(),
        settings.persona_catalog,
    )
    service = TelegramApplicationService(
        router,
        settings.persona_catalog,
        provider,
        context_builder=ContextBuilder(memory_store),
        memory_store=memory_store,
    )
    try:
        await polling(
            bot_token,
            service,
            response_port_factory=AiogramTelegramResponsePort,
        )
    finally:
        conversation_store.close()
        memory_store.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lenkobot")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("login", "run"):
        command_parser = commands.add_parser(command)
        command_parser.add_argument("--config", required=True, type=Path)
        if command == "run":
            command_parser.add_argument("--data-root", type=Path)
    arguments = parser.parse_args(argv)

    try:
        settings = load_runtime_settings(
            arguments.config,
            data_root=getattr(arguments, "data_root", None),
        )
        if arguments.command == "login":
            login(settings)
        else:
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            asyncio.run(run_application(settings, bot_token))
    except (CredentialUnavailable, OSError, ProviderRequestError, ValueError) as error:
        parser.error(str(error))
    return 0
