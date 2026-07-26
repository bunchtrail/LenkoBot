from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import http.client
import ipaddress
import re
import socket
import ssl
from typing import Protocol
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from .security_audit import SecurityAuditEvent, SecurityAuditPort


_ALLOWED_CONTENT_TYPES = frozenset(
    {"application/json", "application/xml", "text/html", "text/plain", "text/xml"}
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_BLOCKED_CODES = frozenset(
    {
        "https_required",
        "invalid_url",
        "private_address",
        "dns_rebinding",
        "unsafe_redirect",
        "unsupported_content_type",
        "body_too_large",
    }
)
_CHARSET_PATTERN = re.compile(r"(?:^|;)\s*charset\s*=\s*[\"']?([^;\"'\s]+)", re.I)
_PRIVATE_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})


@dataclass(frozen=True, slots=True)
class HttpFetchResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class UrlReadResult:
    url: str
    retrieved_at: datetime
    content_type: str
    body: str
    content_sha256: str


class UrlReadError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class DnsResolver(Protocol):
    def resolve(self, hostname: str) -> tuple[str, ...]: ...


class PinnedHttpTransport(Protocol):
    def fetch(
        self,
        *,
        ip_address: str,
        hostname: str,
        request_target: str,
        timeout: float,
        max_body_bytes: int,
    ) -> HttpFetchResponse: ...


class SocketDnsResolver:
    def resolve(self, hostname: str) -> tuple[str, ...]:
        try:
            addresses = socket.getaddrinfo(
                hostname,
                443,
                type=socket.SOCK_STREAM,
            )
        except OSError:
            raise UrlReadError("DNS resolution failed", code="dns_failed") from None
        unique = dict.fromkeys(str(address[4][0]) for address in addresses)
        if not unique:
            raise UrlReadError("DNS resolution returned no address", code="dns_failed")
        return tuple(unique)


class _PinnedHttpsTransport:
    def fetch(
        self,
        *,
        ip_address: str,
        hostname: str,
        request_target: str,
        timeout: float,
        max_body_bytes: int,
    ) -> HttpFetchResponse:
        # One direct socket request keeps DNS pinning explicit; a pooled HTTP
        # client could re-resolve the hostname and bypass the pinned address.
        raw_socket = None
        try:
            raw_socket = socket.create_connection((ip_address, 443), timeout=timeout)
            context = ssl.create_default_context()
            with context.wrap_socket(raw_socket, server_hostname=hostname) as connection:
                raw_socket = None
                host_header = f"[{hostname}]" if ":" in hostname else hostname
                request = (
                    f"GET {request_target} HTTP/1.1\r\n"
                    f"Host: {host_header}\r\n"
                    "Accept: text/html, text/plain, application/json, application/xml\r\n"
                    "Connection: close\r\n"
                    "User-Agent: LenkoBot/0.1\r\n\r\n"
                ).encode("ascii")
                connection.sendall(request)
                response = http.client.HTTPResponse(connection, method="GET")
                response.begin()
                headers = {key.lower(): value for key, value in response.getheaders()}
                content_length = _content_length(headers)
                if content_length is not None and content_length > max_body_bytes:
                    raise UrlReadError("URL response is too large", code="body_too_large")
                body = response.read(max_body_bytes + 1)
                if len(body) > max_body_bytes:
                    raise UrlReadError("URL response is too large", code="body_too_large")
                return HttpFetchResponse(response.status, headers, body)
        except UrlReadError:
            raise
        except (OSError, ssl.SSLError, ValueError, http.client.HTTPException):
            raise UrlReadError("URL request failed", code="network_failed") from None
        finally:
            if raw_socket is not None:
                raw_socket.close()


class ReadOnlyUrlReader:
    def __init__(
        self,
        audit: SecurityAuditPort,
        *,
        resolver: DnsResolver | None = None,
        transport: PinnedHttpTransport | None = None,
        now: Callable[[], datetime] | None = None,
        timeout: float = 10.0,
        max_body_bytes: int = 1_000_000,
        max_redirects: int = 3,
    ) -> None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise ValueError("URL timeout must be numeric")
        if timeout <= 0 or timeout > 60:
            raise ValueError("URL timeout must be between 0 and 60 seconds")
        if isinstance(max_body_bytes, bool) or not isinstance(max_body_bytes, int):
            raise ValueError("URL body limit must be an integer")
        if max_body_bytes < 1 or max_body_bytes > 10_000_000:
            raise ValueError("URL body limit must be between 1 and 10000000")
        if isinstance(max_redirects, bool) or not isinstance(max_redirects, int):
            raise ValueError("URL redirect limit must be an integer")
        if max_redirects < 0 or max_redirects > 10:
            raise ValueError("URL redirect limit must be between 0 and 10")
        self._audit = audit
        self._resolver = resolver or SocketDnsResolver()
        self._transport = transport or _PinnedHttpsTransport()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._timeout = float(timeout)
        self._max_body_bytes = max_body_bytes
        self._max_redirects = max_redirects

    def read(
        self,
        url: str,
        *,
        owner_user_id: int,
        lifecycle_epoch: int,
    ) -> UrlReadResult:
        started_at = _timestamp(self._now())
        action_hash = _url_hash(url)
        try:
            current_url = _normalize_url(url)
            redirects = 0
            while True:
                hostname, request_target = _target_parts(current_url)
                addresses = self._resolver.resolve(hostname)
                if not addresses:
                    raise UrlReadError(
                        "DNS resolution returned no address",
                        code="dns_failed",
                    )
                non_public = [
                    address for address in addresses if not _is_public_ip(address)
                ]
                if non_public:
                    raise UrlReadError(
                        "URL target resolves to a non-public address",
                        code=(
                            "private_address"
                            if len(non_public) == len(addresses)
                            else "dns_rebinding"
                        ),
                    )
                response = self._transport.fetch(
                    ip_address=addresses[0],
                    hostname=hostname,
                    request_target=request_target,
                    timeout=self._timeout,
                    max_body_bytes=self._max_body_bytes,
                )
                if response.status in _REDIRECT_STATUSES:
                    location = _header(response.headers, "location")
                    if not location or redirects >= self._max_redirects:
                        raise UrlReadError(
                            "URL redirect is unsafe or exceeds the limit",
                            code="unsafe_redirect",
                        )
                    current_url = _normalize_url(urljoin(current_url, location))
                    redirects += 1
                    continue
                if not 200 <= response.status < 300:
                    raise UrlReadError("URL request failed", code="network_failed")
                content_type = _content_type(response.headers)
                if content_type not in _ALLOWED_CONTENT_TYPES:
                    raise UrlReadError(
                        "URL content type is not allowed",
                        code="unsupported_content_type",
                    )
                if len(response.body) > self._max_body_bytes:
                    raise UrlReadError("URL response is too large", code="body_too_large")
                body = _decode_body(response.body, response.headers)
                retrieved_at = _as_utc(self._now())
                result = UrlReadResult(
                    url=current_url,
                    retrieved_at=retrieved_at,
                    content_type=content_type,
                    body=body,
                    content_sha256=sha256(response.body).hexdigest(),
                )
                self._record(
                    owner_user_id=owner_user_id,
                    lifecycle_epoch=lifecycle_epoch,
                    action_hash=action_hash,
                    policy_decision="allow",
                    outcome="success",
                    started_at=started_at,
                    finished_at=_timestamp(retrieved_at),
                )
                return result
        except UrlReadError as error:
            if error.code == "audit_unavailable":
                raise
            try:
                self._record(
                    owner_user_id=owner_user_id,
                    lifecycle_epoch=lifecycle_epoch,
                    action_hash=action_hash,
                    policy_decision="deny" if error.code in _BLOCKED_CODES else "allow",
                    outcome="blocked" if error.code in _BLOCKED_CODES else "failed",
                    started_at=started_at,
                    finished_at=_timestamp(self._now()),
                )
            except UrlReadError as audit_error:
                raise audit_error from error
            raise
        except Exception:
            self._record(
                owner_user_id=owner_user_id,
                lifecycle_epoch=lifecycle_epoch,
                action_hash=action_hash,
                policy_decision="allow",
                outcome="failed",
                started_at=started_at,
                finished_at=_timestamp(self._now()),
            )
            raise UrlReadError("URL request failed", code="network_failed") from None

    def _record(
        self,
        *,
        owner_user_id: int,
        lifecycle_epoch: int,
        action_hash: str,
        policy_decision: str,
        outcome: str,
        started_at: str,
        finished_at: str,
    ) -> None:
        try:
            self._audit.record(
                SecurityAuditEvent(
                    owner_user_id=owner_user_id,
                    lifecycle_epoch=lifecycle_epoch,
                    event_type="network_request",
                    capability="url_read",
                    action_hash=action_hash,
                    policy_decision=policy_decision,
                    outcome=outcome,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )
        except Exception:
            raise UrlReadError("Network audit is unavailable", code="audit_unavailable") from None


def _normalize_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(
        ord(character) < 32 for character in value
    ):
        raise UrlReadError("URL is invalid", code="invalid_url")
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise UrlReadError("URL is invalid", code="invalid_url") from None
    if parsed.scheme.casefold() != "https":
        raise UrlReadError("URL must use HTTPS", code="https_required")
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise UrlReadError("URL is invalid", code="invalid_url")
    try:
        hostname = hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        raise UrlReadError("URL is invalid", code="invalid_url") from None
    if hostname in _PRIVATE_HOSTNAMES or not _is_public_hostname(hostname):
        raise UrlReadError("URL target is not public", code="private_address")
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&/%:@!$'()*+,;?-._~")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    return urlunsplit(("https", netloc, path, query, ""))


def _target_parts(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if hostname is None:
        raise UrlReadError("URL is invalid", code="invalid_url")
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    return hostname, target


def _is_public_hostname(hostname: str) -> bool:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return _is_public_ip(address)


def _is_public_ip(value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return None


def _content_type(headers: Mapping[str, str]) -> str:
    value = _header(headers, "content-type")
    if not value:
        return ""
    return value.split(";", 1)[0].strip().casefold()


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = _header(headers, "content-length")
    if value is None:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError):
        raise UrlReadError("URL response is invalid", code="network_failed") from None
    if length < 0:
        raise UrlReadError("URL response is invalid", code="network_failed")
    return length


def _decode_body(body: bytes, headers: Mapping[str, str]) -> str:
    content_type = _header(headers, "content-type") or ""
    match = _CHARSET_PATTERN.search(content_type)
    encoding = match.group(1) if match else "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        raise UrlReadError("URL response encoding is invalid", code="network_failed") from None


def _url_hash(url: str) -> str:
    value = url if isinstance(url, str) else repr(url)
    return sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds")
