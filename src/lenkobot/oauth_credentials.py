from collections.abc import Callable, Mapping
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import math
import re
import sys
import time
from typing import Protocol
from urllib.parse import urlsplit

from .xai_provider import (
    CredentialUnavailable,
    FormHttpClient,
    HttpResponse,
    OAuthCredentialStore,
    OAuthRefreshLock,
    OAuthTokenState,
    ProviderRequestError,
    UrllibJsonHttpClient,
)


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_CRED_MAX_CREDENTIAL_BLOB_SIZE = 2560
_ERROR_NOT_FOUND = 1168
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_AUTH_ENDPOINT_HOSTS = frozenset({"auth.x.ai"})
_VERIFICATION_HOSTS = frozenset({"auth.x.ai", "accounts.x.ai"})


class _CredentialW(ctypes.Structure):
    _fields_ = (
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    )


_CredentialPointer = ctypes.POINTER(_CredentialW)


class CredentialManagerApi(Protocol):
    def read(self, target_name: str) -> bytes | None: ...

    def write(self, target_name: str, blob: bytes) -> None: ...


class _WindowsCredentialManagerApi:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise CredentialUnavailable("Windows Credential Manager is unavailable")
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._cred_read = advapi32.CredReadW
        self._cred_read.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_CredentialPointer),
        )
        self._cred_read.restype = wintypes.BOOL
        self._cred_write = advapi32.CredWriteW
        self._cred_write.argtypes = (ctypes.POINTER(_CredentialW), wintypes.DWORD)
        self._cred_write.restype = wintypes.BOOL
        self._cred_free = advapi32.CredFree
        self._cred_free.argtypes = (ctypes.c_void_p,)
        self._cred_free.restype = None

    def read(self, target_name: str) -> bytes | None:
        credential_pointer = _CredentialPointer()
        if not self._cred_read(
            target_name,
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(credential_pointer),
        ):
            error_code = ctypes.get_last_error()
            if error_code == _ERROR_NOT_FOUND:
                return None
            raise CredentialUnavailable(
                f"Windows Credential Manager read failed ({error_code})"
            )
        try:
            credential = credential_pointer.contents
            return ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
        finally:
            self._cred_free(ctypes.cast(credential_pointer, ctypes.c_void_p))

    def write(self, target_name: str, blob: bytes) -> None:
        blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = _CredentialW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = target_name
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(
            blob_buffer,
            ctypes.POINTER(ctypes.c_ubyte),
        )
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "xai-oauth"
        if not self._cred_write(ctypes.byref(credential), 0):
            error_code = ctypes.get_last_error()
            raise CredentialUnavailable(
                f"Windows Credential Manager write failed ({error_code})"
            )


class WindowsOAuthCredentialStore:
    def __init__(
        self,
        *,
        profile_id: str = "default",
        api: CredentialManagerApi | None = None,
    ) -> None:
        if not _PROFILE_ID_PATTERN.fullmatch(profile_id):
            raise ValueError("OAuth credential profile ID is invalid")
        self._target_name = f"LenkoBot/xai-oauth/v1/{profile_id}"
        self._api = api or _WindowsCredentialManagerApi()

    @property
    def target_name(self) -> str:
        return self._target_name

    def load(self) -> OAuthTokenState | None:
        try:
            blob = self._api.read(self._target_name)
        except CredentialUnavailable:
            raise
        except Exception:
            raise CredentialUnavailable(
                "Windows Credential Manager could not read OAuth state"
            ) from None
        if blob is None:
            return None
        if len(blob) > _CRED_MAX_CREDENTIAL_BLOB_SIZE:
            raise CredentialUnavailable("Stored OAuth credential is too large")
        try:
            payload = json.loads(blob.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "access_token",
                "expires_at",
                "refresh_token",
            }:
                raise ValueError
            expires_at = datetime.fromisoformat(payload["expires_at"])
            state = OAuthTokenState(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                expires_at=expires_at,
            )
            self._validate_state(state)
        except (AttributeError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise CredentialUnavailable("Stored OAuth credential is invalid") from None
        return state

    def save(self, state: OAuthTokenState) -> None:
        self._validate_state(state)
        blob = json.dumps(
            {
                "access_token": state.access_token,
                "expires_at": state.expires_at.astimezone(timezone.utc).isoformat(),
                "refresh_token": state.refresh_token,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(blob) > _CRED_MAX_CREDENTIAL_BLOB_SIZE:
            raise CredentialUnavailable("OAuth credential is too large to store")
        try:
            self._api.write(self._target_name, blob)
        except CredentialUnavailable:
            raise
        except Exception:
            raise CredentialUnavailable(
                "Windows Credential Manager could not write OAuth state"
            ) from None

    @staticmethod
    def _validate_state(state: OAuthTokenState) -> None:
        if not isinstance(state, OAuthTokenState):
            raise CredentialUnavailable("OAuth credential state is invalid")
        if not isinstance(state.access_token, str) or not state.access_token.strip():
            raise CredentialUnavailable("OAuth access token is empty")
        if not isinstance(state.refresh_token, str) or not state.refresh_token.strip():
            raise CredentialUnavailable("OAuth refresh token is empty")
        if not isinstance(state.expires_at, datetime) or state.expires_at.tzinfo is None:
            raise CredentialUnavailable("OAuth token expiry must include a timezone")


class MutexWaitResult(StrEnum):
    ACQUIRED = "acquired"
    ABANDONED = "abandoned"
    TIMEOUT = "timeout"
    FAILED = "failed"


class WindowsMutexApi(Protocol):
    def create(self, name: str) -> object: ...

    def wait(self, handle: object, timeout_ms: int) -> MutexWaitResult: ...

    def release(self, handle: object) -> None: ...

    def close(self, handle: object) -> None: ...


class _WindowsMutexApi:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise CredentialUnavailable("Windows named mutex is unavailable")
        kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        self._create_mutex = kernel32.CreateMutexW
        self._create_mutex.argtypes = (
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        self._create_mutex.restype = wintypes.HANDLE
        self._wait_for_single_object = kernel32.WaitForSingleObject
        self._wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self._wait_for_single_object.restype = wintypes.DWORD
        self._release_mutex = kernel32.ReleaseMutex
        self._release_mutex.argtypes = (wintypes.HANDLE,)
        self._release_mutex.restype = wintypes.BOOL
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = (wintypes.HANDLE,)
        self._close_handle.restype = wintypes.BOOL

    def create(self, name: str) -> object:
        handle = self._create_mutex(None, False, name)
        if not handle:
            error_code = ctypes.get_last_error()
            raise CredentialUnavailable(
                f"Windows OAuth mutex creation failed ({error_code})"
            )
        return handle

    def wait(self, handle: object, timeout_ms: int) -> MutexWaitResult:
        result = self._wait_for_single_object(handle, timeout_ms)
        if result == _WAIT_OBJECT_0:
            return MutexWaitResult.ACQUIRED
        if result == _WAIT_ABANDONED:
            return MutexWaitResult.ABANDONED
        if result == _WAIT_TIMEOUT:
            return MutexWaitResult.TIMEOUT
        if result == _WAIT_FAILED:
            return MutexWaitResult.FAILED
        return MutexWaitResult.FAILED

    def release(self, handle: object) -> None:
        if not self._release_mutex(handle):
            error_code = ctypes.get_last_error()
            raise CredentialUnavailable(
                f"Windows OAuth mutex release failed ({error_code})"
            )

    def close(self, handle: object) -> None:
        if not self._close_handle(handle):
            error_code = ctypes.get_last_error()
            raise CredentialUnavailable(
                f"Windows OAuth mutex close failed ({error_code})"
            )


class WindowsOAuthRefreshMutex:
    def __init__(
        self,
        target_name: str,
        *,
        api: WindowsMutexApi | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        if not target_name.strip():
            raise ValueError("OAuth credential target name cannot be empty")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("OAuth mutex timeout must be positive")
        timeout_ms = int(timeout_seconds * 1000)
        if timeout_ms <= 0 or timeout_ms >= _WAIT_FAILED:
            raise ValueError("OAuth mutex timeout is out of range")
        target_hash = hashlib.sha256(target_name.encode("utf-8")).hexdigest()
        self._name = f"Local\\LenkoBot.XaiOAuth.Refresh.{target_hash}"
        self._api = api or _WindowsMutexApi()
        self._timeout_ms = timeout_ms
        self._handle: object | None = None

    def __enter__(self) -> "WindowsOAuthRefreshMutex":
        if self._handle is not None:
            raise RuntimeError("OAuth mutex is already acquired")
        try:
            handle = self._api.create(self._name)
            result = self._api.wait(handle, self._timeout_ms)
        except CredentialUnavailable:
            if "handle" in locals():
                self._close_safely(handle)
            raise
        except Exception:
            if "handle" in locals():
                self._close_safely(handle)
            raise CredentialUnavailable("Windows OAuth mutex acquisition failed") from None
        if result in (MutexWaitResult.ACQUIRED, MutexWaitResult.ABANDONED):
            self._handle = handle
            return self
        self._close_safely(handle)
        if result is MutexWaitResult.TIMEOUT:
            raise CredentialUnavailable("Windows OAuth mutex timed out")
        raise CredentialUnavailable("Windows OAuth mutex wait failed")

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            self._api.release(handle)
        except Exception:
            self._close_safely(handle)
            raise CredentialUnavailable("Windows OAuth mutex release failed") from None
        try:
            self._api.close(handle)
        except Exception:
            raise CredentialUnavailable("Windows OAuth mutex close failed") from None

    def _close_safely(self, handle: object) -> None:
        try:
            self._api.close(handle)
        except Exception:
            pass


@dataclass(frozen=True, slots=True)
class OAuthDeviceAuthorization:
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None = field(repr=False)
    expires_at: datetime
    interval_seconds: float


class XaiOAuthDeviceClient:
    DEFAULT_SCOPES = (
        "openid",
        "profile",
        "email",
        "offline_access",
        "grok-cli:access",
        "api:access",
    )

    def __init__(
        self,
        *,
        client_id: str,
        scopes: tuple[str, ...] = DEFAULT_SCOPES,
        http_client: FormHttpClient | None = None,
        device_url: str = "https://auth.x.ai/oauth2/device/code",
        token_url: str = "https://auth.x.ai/oauth2/token",
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not client_id.strip():
            raise ValueError("OAuth client ID cannot be empty")
        normalized_scopes = tuple(scope.strip() for scope in scopes)
        if not normalized_scopes or any(not scope for scope in normalized_scopes):
            raise ValueError("OAuth scopes cannot be empty")
        self._validate_url(
            device_url,
            allowed_hosts=_AUTH_ENDPOINT_HOSTS,
            allow_query=False,
        )
        self._validate_url(
            token_url,
            allowed_hosts=_AUTH_ENDPOINT_HOSTS,
            allow_query=False,
        )
        self._client_id = client_id
        self._scopes = normalized_scopes
        self._http_client = http_client or UrllibJsonHttpClient()
        self._device_url = device_url
        self._token_url = token_url
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or time.sleep

    def start_device_authorization(self) -> OAuthDeviceAuthorization:
        response = self._http_client.post_form(
            self._device_url,
            self._form_headers(),
            {
                "client_id": self._client_id,
                "scope": " ".join(self._scopes),
            },
        )
        body = self._decode_response(response)
        if not 200 <= response.status < 300:
            raise self._request_error(response, body)
        device_code = body.get("device_code")
        user_code = body.get("user_code")
        verification_uri = body.get("verification_uri")
        verification_uri_complete = body.get("verification_uri_complete")
        expires_in = self._positive_number(body.get("expires_in"))
        interval = self._positive_number(body.get("interval", 5))
        if not isinstance(device_code, str) or not device_code.strip():
            raise CredentialUnavailable("OAuth device response has no device code")
        if not isinstance(user_code, str) or not user_code.strip():
            raise CredentialUnavailable("OAuth device response has no user code")
        if not isinstance(verification_uri, str) or not verification_uri.strip():
            raise CredentialUnavailable("OAuth device response has no verification URI")
        self._validate_verification_url(
            verification_uri,
            allow_query=False,
        )
        if verification_uri_complete is not None:
            if (
                not isinstance(verification_uri_complete, str)
                or not verification_uri_complete.strip()
            ):
                raise CredentialUnavailable(
                    "OAuth device response has an invalid verification URI"
                )
            self._validate_verification_url(
                verification_uri_complete,
                allow_query=True,
            )
        now = self._aware_now()
        return OAuthDeviceAuthorization(
            device_code=device_code,
            user_code=user_code,
            verification_uri=verification_uri,
            verification_uri_complete=verification_uri_complete,
            expires_at=now + timedelta(seconds=expires_in),
            interval_seconds=interval,
        )

    def complete_device_authorization(
        self,
        authorization: OAuthDeviceAuthorization,
        *,
        store: OAuthCredentialStore,
        lock: OAuthRefreshLock,
    ) -> OAuthTokenState:
        if store is None or lock is None:
            raise ValueError("OAuth device persistence dependencies are required")
        state = self._poll_for_token(authorization)
        with lock:
            store.save(state)
        return state

    def _poll_for_token(
        self,
        authorization: OAuthDeviceAuthorization,
    ) -> OAuthTokenState:
        self._validate_authorization(authorization)
        interval = authorization.interval_seconds
        while True:
            now = self._aware_now()
            if now >= authorization.expires_at:
                raise CredentialUnavailable("OAuth device authorization expired")
            self._sleep(interval)
            now = self._aware_now()
            if now >= authorization.expires_at:
                raise CredentialUnavailable("OAuth device authorization expired")
            response = self._http_client.post_form(
                self._token_url,
                self._form_headers(),
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": authorization.device_code,
                    "client_id": self._client_id,
                },
            )
            body = self._decode_response(response)
            if 200 <= response.status < 300:
                return self._token_state(body, now)
            error = self._request_error(response, body)
            if error.code == "authorization_pending":
                continue
            if error.code == "slow_down":
                interval += 5
                continue
            if error.code in ("access_denied", "expired_token"):
                raise CredentialUnavailable("OAuth device authorization did not complete")
            raise error

    @staticmethod
    def _validate_authorization(authorization: OAuthDeviceAuthorization) -> None:
        if not isinstance(authorization, OAuthDeviceAuthorization):
            raise ValueError("OAuth device authorization is invalid")
        if (
            not isinstance(authorization.device_code, str)
            or not authorization.device_code.strip()
        ):
            raise CredentialUnavailable("OAuth device code is empty")
        if (
            not isinstance(authorization.expires_at, datetime)
            or authorization.expires_at.tzinfo is None
        ):
            raise CredentialUnavailable("OAuth device expiry must include a timezone")
        if (
            isinstance(authorization.interval_seconds, bool)
            or not isinstance(authorization.interval_seconds, (int, float))
            or not math.isfinite(authorization.interval_seconds)
            or authorization.interval_seconds <= 0
        ):
            raise CredentialUnavailable("OAuth device poll interval is invalid")

    def _token_state(
        self,
        body: Mapping[str, object],
        now: datetime,
    ) -> OAuthTokenState:
        access_token = body.get("access_token")
        refresh_token = body.get("refresh_token")
        expires_in = self._positive_number(body.get("expires_in"))
        if not isinstance(access_token, str) or not access_token.strip():
            raise CredentialUnavailable("OAuth device token response has no access token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            raise CredentialUnavailable("OAuth device token response has no refresh token")
        return OAuthTokenState(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=now + timedelta(seconds=expires_in),
        )

    def _aware_now(self) -> datetime:
        now = self._now()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise CredentialUnavailable("OAuth clock must include a timezone")
        return now

    @staticmethod
    def _positive_number(value: object) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise CredentialUnavailable("OAuth response has an invalid duration")
        return float(value)

    @classmethod
    def _validate_verification_url(cls, url: str, *, allow_query: bool) -> None:
        try:
            cls._validate_url(
                url,
                allowed_hosts=_VERIFICATION_HOSTS,
                allow_query=allow_query,
            )
        except ValueError:
            raise CredentialUnavailable(
                "OAuth device response has an invalid verification URI"
            ) from None

    @staticmethod
    def _form_headers() -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    @staticmethod
    def _decode_response(response: HttpResponse) -> dict[str, object]:
        try:
            body = json.loads(response.body)
        except json.JSONDecodeError as error:
            raise ProviderRequestError(
                "xAI OAuth endpoint returned invalid JSON",
                status=response.status,
                code="invalid_json",
                raw_body="",
                headers={},
            ) from error
        if not isinstance(body, dict):
            raise ProviderRequestError(
                "xAI OAuth endpoint returned an invalid response",
                status=response.status,
                code="invalid_json",
                raw_body="",
                headers={},
            )
        return body

    @staticmethod
    def _request_error(
        response: HttpResponse,
        body: Mapping[str, object],
    ) -> ProviderRequestError:
        error_code = body.get("error")
        if isinstance(error_code, dict):
            nested_code = error_code.get("code")
            error_code = nested_code if isinstance(nested_code, str) else None
        return ProviderRequestError(
            "xAI OAuth request failed",
            status=response.status,
            code=error_code if isinstance(error_code, str) else None,
            raw_body="",
            headers={},
        )

    @staticmethod
    def _validate_url(
        url: str,
        *,
        allowed_hosts: frozenset[str],
        allow_query: bool,
    ) -> None:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise ValueError("OAuth target is not an allowed HTTPS host") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname not in allowed_hosts
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or (parsed.query and not allow_query)
            or parsed.fragment
        ):
            raise ValueError("OAuth target is not an allowed HTTPS host")
