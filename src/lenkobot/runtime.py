import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from getpass import getpass
import os
from pathlib import Path
import tomllib

from .aiogram_adapter import (
    AiogramTelegramReplyResponsePort,
    AiogramTelegramResponsePort,
    run_polling,
    verify_bot_identity,
)
from .application_service import TelegramApplicationService
from .context_builder import ContextBuilder
from .live_smoke import run_live_smoke
from .memory import SQLiteMemoryStore
from .memory_extraction import MemoryExtractionService
from .oauth_credentials import (
    WindowsOAuthCredentialStore,
    WindowsOAuthRefreshMutex,
    XaiOAuthDeviceClient,
)
from .personas import PersonaCatalog
from .session_store import SQLiteSessionFinalizer, SQLiteSessionStore
from .session_summary import XaiSummaryGenerator
from .telegram_e2e import (
    TelegramE2EError,
    load_telegram_e2e_settings,
    prepare_telegram_e2e_bot_data_root,
    run_telegram_e2e,
)
from .telegram_e2e_credentials import (
    TelegramE2ECredentialError,
    TelegramE2ECredentialState,
    WindowsTelegramE2ECredentialStore,
)
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
    XaiStructuredProvider,
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
    export_recipient: str | None = None


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
    export = data.get("export")
    if not isinstance(telegram, dict):
        raise ValueError("runtime configuration must contain a telegram table")
    if oauth is not None and not isinstance(oauth, dict):
        raise ValueError("runtime configuration oauth value must be a table")
    if export is not None and not isinstance(export, dict):
        raise ValueError("runtime configuration export value must be a table")

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

    export_recipient = export.get("age_recipient") if isinstance(export, dict) else None
    if export_recipient is not None and (
        not isinstance(export_recipient, str)
        or not export_recipient.startswith("age1")
        or len(export_recipient) < 10
        or any(character.isspace() for character in export_recipient)
    ):
        raise ValueError("export age_recipient is invalid")

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
        export_recipient=export_recipient,
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


def login_telegram_e2e(
    *,
    authorize: Callable[..., Awaitable[TelegramE2ECredentialState]],
    store: WindowsTelegramE2ECredentialStore,
    expected_user_id: int,
    input_value: Callable[[str], str] = input,
    secret_input: Callable[[str], str] = getpass,
    output: Callable[[str], None] = print,
) -> None:
    try:
        api_id = int(input_value("Telegram API ID: ").strip())
    except (TypeError, ValueError):
        raise ValueError("Telegram API ID must be a positive integer") from None
    if api_id <= 0:
        raise ValueError("Telegram API ID must be a positive integer")
    api_hash = secret_input("Telegram API hash: ").strip()
    if len(api_hash) != 32 or any(
        character not in "0123456789abcdefABCDEF" for character in api_hash
    ):
        raise ValueError("Telegram API hash is invalid")
    phone = input_value("Test account phone: ").strip()
    if not phone:
        raise ValueError("Telegram test account phone cannot be empty")

    state = asyncio.run(
        authorize(
            api_id=api_id,
            api_hash=api_hash,
            phone=phone,
            code_provider=lambda: secret_input("Telegram login code: ").strip(),
            password_provider=lambda: secret_input(
                "Telegram 2FA password: "
            ).strip(),
        )
    )
    if state.user_id != expected_user_id:
        raise ValueError(
            "Telegram login does not match the configured test user"
        )
    store.save(state)
    output(f"Telegram E2E login completed for test user ID {state.user_id}.")


async def run_application(
    settings: RuntimeSettings,
    bot_token: str,
    *,
    polling: Callable[..., Awaitable[None]] = run_polling,
    response_port_factory: Callable[..., object] = AiogramTelegramResponsePort,
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
    transport = XaiResponsesTransport()
    oauth_source = OAuthCredentialSource(coordinator, base_url=_INFERENCE_BASE_URL)
    provider = XaiProvider(
        transport,
        CredentialPolicy.OAUTH_ONLY,
        oauth_source=oauth_source,
        model=_MODEL,
    )
    structured_provider = XaiStructuredProvider(
        transport,
        oauth_source=oauth_source,
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
    try:
        session_store = SQLiteSessionStore(database_path)
    except Exception:
        conversation_store.close()
        memory_store.close()
        raise
    extraction_service = MemoryExtractionService(
        memory_store,
        session_store,
        structured_provider,
    )
    session_finalizer = SQLiteSessionFinalizer(
        database_path,
        XaiSummaryGenerator(structured_provider),
        extraction_store=memory_store,
        extraction_processor=extraction_service,
    )

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
        context_builder=ContextBuilder(
            memory_store,
            transcript_store=session_store,
        ),
        memory_store=memory_store,
        session_store=session_store,
        extraction_service=extraction_service,
        session_finalizer=session_finalizer,
    )
    try:
        await polling(
            bot_token,
            service,
            response_port_factory=response_port_factory,
        )
    finally:
        conversation_store.close()
        memory_store.close()
        session_store.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lenkobot")
    commands = parser.add_subparsers(dest="command", required=True)
    e2e_login_parser = commands.add_parser("telegram-e2e-login")
    e2e_login_parser.add_argument("--config", required=True, type=Path)
    for command in (
        "login",
        "run",
        "live-smoke",
        "telegram-e2e",
        "telegram-e2e-bot",
    ):
        command_parser = commands.add_parser(command)
        command_parser.add_argument("--config", required=True, type=Path)
        if command == "run":
            command_parser.add_argument("--data-root", type=Path)
        elif command == "live-smoke":
            command_parser.add_argument("--data-root", required=True, type=Path)
            command_parser.add_argument("--confirm-send", action="store_true")
        elif command == "telegram-e2e":
            command_parser.add_argument("--confirm-send", action="store_true")
        elif command == "telegram-e2e-bot":
            command_parser.add_argument("--data-root", required=True, type=Path)
            command_parser.add_argument("--confirm-run", action="store_true")
    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "telegram-e2e-login":
            settings = load_telegram_e2e_settings(arguments.config)
            authorize, _ = _load_telethon_e2e_adapters()
            login_telegram_e2e(
                authorize=authorize,
                store=WindowsTelegramE2ECredentialStore(),
                expected_user_id=settings.allowed_user_id,
            )
        elif arguments.command == "telegram-e2e":
            settings = load_telegram_e2e_settings(arguments.config)
            credentials = WindowsTelegramE2ECredentialStore().load()
            if credentials is None:
                raise TelegramE2ECredentialError(
                    "Telegram E2E credential state is unavailable"
                )
            _, transport_factory = _load_telethon_e2e_adapters()
            report = asyncio.run(
                run_telegram_e2e(
                    settings,
                    credentials,
                    confirmed=arguments.confirm_send,
                    transport_factory=transport_factory,
                )
            )
            for step in report.steps:
                print(f"{step.command} -> {step.response_text}")
            print(
                "Telegram E2E completed: "
                f"{report.command_count} replies received and verified."
            )
        elif arguments.command == "telegram-e2e-bot":
            if not arguments.confirm_run:
                raise ValueError(
                    "Telegram E2E bot requires explicit run confirmation"
                )
            e2e_settings = load_telegram_e2e_settings(arguments.config)
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            asyncio.run(
                verify_bot_identity(
                    bot_token,
                    expected_bot_user_id=e2e_settings.bot_user_id,
                )
            )
            data_root = prepare_telegram_e2e_bot_data_root(
                arguments.data_root,
                config_path=arguments.config,
            )
            settings = load_runtime_settings(
                arguments.config,
                data_root=data_root,
            )
            asyncio.run(
                run_application(
                    settings,
                    bot_token,
                    response_port_factory=AiogramTelegramReplyResponsePort,
                )
            )
        else:
            settings = load_runtime_settings(
                arguments.config,
                data_root=getattr(arguments, "data_root", None),
            )
        if arguments.command == "login":
            login(settings)
        elif arguments.command == "run":
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            asyncio.run(run_application(settings, bot_token))
        elif arguments.command == "live-smoke":
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            report = asyncio.run(
                run_live_smoke(
                    settings,
                    bot_token,
                    config_path=arguments.config,
                    confirmed=arguments.confirm_send,
                )
            )
            print(
                "Telegram live smoke completed: "
                f"{report.command_count} commands delivered."
            )
    except (
        CredentialUnavailable,
        OSError,
        ProviderRequestError,
        TelegramE2ECredentialError,
        TelegramE2EError,
        ValueError,
    ) as error:
        parser.error(str(error))
    return 0


def _load_telethon_e2e_adapters() -> tuple[Callable[..., object], Callable[..., object]]:
    from .telethon_e2e import authorize_telethon_user, open_telethon_transport

    return authorize_telethon_user, open_telethon_transport
