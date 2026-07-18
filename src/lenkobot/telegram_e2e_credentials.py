from dataclasses import dataclass, field
import json

from .oauth_credentials import (
    CREDENTIAL_MAX_BLOB_SIZE,
    CredentialManagerApi,
    WindowsCredentialManagerApi,
)


_TARGET_NAME = "LenkoBot/telegram-e2e/v1/default"


class TelegramE2ECredentialError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramE2ECredentialState:
    api_id: int
    api_hash: str = field(repr=False)
    session: str = field(repr=False)
    user_id: int

    def __post_init__(self) -> None:
        _validate_state(self)


class WindowsTelegramE2ECredentialStore:
    def __init__(self, *, api: CredentialManagerApi | None = None) -> None:
        self._api = api or WindowsCredentialManagerApi(username="telegram-e2e")

    @property
    def target_name(self) -> str:
        return _TARGET_NAME

    def load(self) -> TelegramE2ECredentialState | None:
        try:
            blob = self._api.read(_TARGET_NAME)
        except Exception:
            raise TelegramE2ECredentialError(
                "Windows Credential Manager could not read Telegram E2E state"
            ) from None
        if blob is None:
            return None
        if len(blob) > CREDENTIAL_MAX_BLOB_SIZE:
            raise TelegramE2ECredentialError(
                "Stored Telegram E2E credential is too large"
            )
        try:
            payload = json.loads(blob.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "api_hash",
                "api_id",
                "session",
                "user_id",
            }:
                raise ValueError
            state = TelegramE2ECredentialState(
                api_id=payload["api_id"],
                api_hash=payload["api_hash"],
                session=payload["session"],
                user_id=payload["user_id"],
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TelegramE2ECredentialError,
        ):
            raise TelegramE2ECredentialError(
                "Stored Telegram E2E credential is invalid"
            ) from None
        return state

    def save(self, state: TelegramE2ECredentialState) -> None:
        _validate_state(state)
        blob = json.dumps(
            {
                "api_hash": state.api_hash,
                "api_id": state.api_id,
                "session": state.session,
                "user_id": state.user_id,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(blob) > CREDENTIAL_MAX_BLOB_SIZE:
            raise TelegramE2ECredentialError(
                "Telegram E2E credential is too large to store"
            )
        try:
            self._api.write(_TARGET_NAME, blob)
        except Exception:
            raise TelegramE2ECredentialError(
                "Windows Credential Manager could not write Telegram E2E state"
            ) from None


def _validate_state(state: TelegramE2ECredentialState) -> None:
    if not isinstance(state, TelegramE2ECredentialState):
        raise TelegramE2ECredentialError("Telegram E2E credential state is invalid")
    if isinstance(state.api_id, bool) or not isinstance(state.api_id, int) or state.api_id <= 0:
        raise TelegramE2ECredentialError("Telegram E2E API ID is invalid")
    if (
        not isinstance(state.api_hash, str)
        or len(state.api_hash) != 32
        or any(character not in "0123456789abcdefABCDEF" for character in state.api_hash)
    ):
        raise TelegramE2ECredentialError("Telegram E2E API hash is invalid")
    if not isinstance(state.session, str) or not state.session.strip():
        raise TelegramE2ECredentialError("Telegram E2E session is invalid")
    if isinstance(state.user_id, bool) or not isinstance(state.user_id, int) or state.user_id <= 0:
        raise TelegramE2ECredentialError("Telegram E2E user ID is invalid")
