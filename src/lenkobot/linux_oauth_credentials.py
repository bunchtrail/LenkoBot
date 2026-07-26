from datetime import datetime, timezone
import errno
import json
import math
import os
from pathlib import Path
import secrets
import stat
import time
from typing import Protocol

from .xai_provider import CredentialUnavailable, OAuthTokenState


CREDENTIAL_MAX_BLOB_SIZE = 2560
PRIVATE_FILE_MODE = 0o600

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
_SYMLINK_ERRNOS = frozenset({errno.ELOOP, errno.EMLINK})


def linux_credential_path(*, home: Path | str | None = None) -> Path:
    """Resolve the protected OAuth state path outside the repository and SQLite.

    `XDG_CONFIG_HOME` wins when it is an absolute path, matching the XDG spec,
    which requires relative values to be ignored.
    """
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(configured) if configured else None
    if base is None or not base.is_absolute():
        base = Path(home) if home is not None else Path.home()
        base = base / ".config"
    return base / "lenkobot" / "xai-oauth.json"


class PosixSecurityApi(Protocol):
    def current_uid(self) -> int: ...

    def descriptor_owner_uid(self, descriptor: int) -> int: ...

    def descriptor_mode(self, descriptor: int) -> int: ...

    def lock_exclusive(self, descriptor: int) -> bool: ...

    def unlock(self, descriptor: int) -> None: ...


class FcntlPosixSecurity:
    """Real POSIX ownership, permission and advisory-lock primitives."""

    def __init__(self) -> None:
        try:
            import fcntl
        except ImportError:
            raise CredentialUnavailable(
                "POSIX credential protection is unavailable on this platform"
            ) from None
        self._fcntl = fcntl

    def current_uid(self) -> int:
        return os.getuid()

    def descriptor_owner_uid(self, descriptor: int) -> int:
        return os.fstat(descriptor).st_uid

    def descriptor_mode(self, descriptor: int) -> int:
        return stat.S_IMODE(os.fstat(descriptor).st_mode)

    def lock_exclusive(self, descriptor: int) -> bool:
        try:
            self._fcntl.flock(descriptor, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise CredentialUnavailable("OAuth refresh lock failed") from None
        return True

    def unlock(self, descriptor: int) -> None:
        try:
            self._fcntl.flock(descriptor, self._fcntl.LOCK_UN)
        except OSError:
            raise CredentialUnavailable("OAuth refresh lock release failed") from None


class LinuxOAuthCredentialStore:
    """Stores rotating OAuth state in a service-owned `0600` file.

    Ownership and permissions are checked on the open descriptor rather than on
    the path, so a credential file cannot be swapped between validation and read.
    """

    def __init__(
        self,
        *,
        path: Path | str,
        security: PosixSecurityApi | None = None,
    ) -> None:
        resolved = Path(path)
        if not resolved.name.strip():
            raise ValueError("OAuth credential path must name a file")
        self._path = resolved
        self._security = security or FcntlPosixSecurity()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def target_name(self) -> str:
        return str(self._path)

    def load(self) -> OAuthTokenState | None:
        try:
            descriptor = os.open(
                self._path,
                os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY,
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno in _SYMLINK_ERRNOS:
                raise CredentialUnavailable(
                    "OAuth credential path is a symbolic link"
                ) from None
            raise CredentialUnavailable("OAuth credential file is unreadable") from None
        try:
            self._verify_descriptor(descriptor)
            blob = self._read_all(descriptor)
        finally:
            os.close(descriptor)
        if len(blob) > CREDENTIAL_MAX_BLOB_SIZE:
            raise CredentialUnavailable("Stored OAuth credential is too large")
        return self._decode(blob)

    def save(self, state: OAuthTokenState) -> None:
        blob = self._encode(state)
        directory = self._path.parent
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError:
            raise CredentialUnavailable(
                "OAuth credential directory could not be created"
            ) from None
        temporary = directory / f".{self._path.name}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY,
                PRIVATE_FILE_MODE,
            )
        except OSError:
            raise CredentialUnavailable(
                "OAuth credential file could not be created"
            ) from None
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, PRIVATE_FILE_MODE)
            self._write_all(descriptor, blob)
            os.fsync(descriptor)
        except OSError:
            os.close(descriptor)
            self._discard(temporary)
            raise CredentialUnavailable("OAuth credential file could not be written") from None
        except BaseException:
            os.close(descriptor)
            self._discard(temporary)
            raise
        os.close(descriptor)
        try:
            os.replace(temporary, self._path)
        except OSError:
            self._discard(temporary)
            raise CredentialUnavailable("OAuth credential file could not be replaced") from None
        self._sync_directory(directory)

    def _verify_descriptor(self, descriptor: int) -> None:
        try:
            file_stat = os.fstat(descriptor)
        except OSError:
            raise CredentialUnavailable("OAuth credential file is unreadable") from None
        if not stat.S_ISREG(file_stat.st_mode):
            raise CredentialUnavailable("OAuth credential path is not a regular file")
        if self._security.descriptor_mode(descriptor) != PRIVATE_FILE_MODE:
            raise CredentialUnavailable(
                "OAuth credential file must be readable only by its owner"
            )
        if self._security.descriptor_owner_uid(descriptor) != self._security.current_uid():
            raise CredentialUnavailable("OAuth credential file is owned by another user")

    @staticmethod
    def _read_all(descriptor: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = os.read(descriptor, 4096)
            except OSError:
                raise CredentialUnavailable("OAuth credential file is unreadable") from None
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > CREDENTIAL_MAX_BLOB_SIZE:
                return b"".join(chunks)

    @staticmethod
    def _write_all(descriptor: int, blob: bytes) -> None:
        written = 0
        while written < len(blob):
            written += os.write(descriptor, blob[written:])

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | _O_CLOEXEC)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    @classmethod
    def _decode(cls, blob: bytes) -> OAuthTokenState:
        try:
            payload = json.loads(blob.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "access_token",
                "expires_at",
                "refresh_token",
            }:
                raise ValueError
            state = OAuthTokenState(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                expires_at=datetime.fromisoformat(payload["expires_at"]),
            )
            cls._validate_state(state)
        except (AttributeError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise CredentialUnavailable("Stored OAuth credential is invalid") from None
        return state

    @classmethod
    def _encode(cls, state: OAuthTokenState) -> bytes:
        cls._validate_state(state)
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
        if len(blob) > CREDENTIAL_MAX_BLOB_SIZE:
            raise CredentialUnavailable("OAuth credential is too large to store")
        return blob

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


class LinuxOAuthRefreshLock:
    """Advisory exclusive lock that serializes OAuth refresh across processes."""

    def __init__(
        self,
        credential_path: Path | str,
        *,
        security: PosixSecurityApi | None = None,
        timeout_seconds: float = 10,
        poll_seconds: float = 0.05,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("OAuth lock timeout must be positive")
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or not math.isfinite(poll_seconds)
            or poll_seconds <= 0
        ):
            raise ValueError("OAuth lock poll interval must be positive")
        path = Path(credential_path)
        self._path = path.with_name(f"{path.name}.lock")
        self._security = security or FcntlPosixSecurity()
        self._timeout_seconds = float(timeout_seconds)
        self._poll_seconds = float(poll_seconds)
        self._descriptor: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def __enter__(self) -> "LinuxOAuthRefreshLock":
        if self._descriptor is not None:
            raise RuntimeError("OAuth lock is already acquired")
        try:
            descriptor = os.open(
                self._path,
                os.O_CREAT | os.O_RDWR | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY,
                PRIVATE_FILE_MODE,
            )
        except OSError:
            raise CredentialUnavailable("OAuth lock file could not be opened") from None
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            try:
                acquired = self._security.lock_exclusive(descriptor)
            except BaseException:
                os.close(descriptor)
                raise
            if acquired:
                self._descriptor = descriptor
                return self
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise CredentialUnavailable("OAuth refresh lock timed out")
            time.sleep(self._poll_seconds)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            self._security.unlock(descriptor)
        finally:
            os.close(descriptor)
