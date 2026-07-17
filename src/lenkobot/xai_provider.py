from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
import json
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class BearerCredential:
    token: str = field(repr=False)
    expires_at: datetime | None
    base_url: str
    source_identity: str


@dataclass(frozen=True, slots=True)
class OAuthAccessToken:
    value: str = field(repr=False)
    expires_at: datetime


class CredentialUnavailable(RuntimeError):
    pass


class ApiKeyCredentialSource:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.x.ai/v1",
    ) -> None:
        if not api_key.strip():
            raise ValueError("xAI API key cannot be empty")

        self._api_key = api_key
        self._base_url = base_url

    def get_credential(self) -> BearerCredential:
        return BearerCredential(
            token=self._api_key,
            expires_at=None,
            base_url=self._base_url,
            source_identity="xai_api_key",
        )


class OAuthCredentialSource:
    def __init__(
        self,
        load_access_token: Callable[[], OAuthAccessToken],
        *,
        base_url: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("OAuth inference base URL cannot be empty")

        self._load_access_token = load_access_token
        self._base_url = base_url
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get_credential(self) -> BearerCredential:
        access_token = self._load_access_token()
        if access_token.expires_at.tzinfo is None:
            raise CredentialUnavailable("OAuth token expiry must include a timezone")
        if access_token.expires_at <= self._now():
            raise CredentialUnavailable("OAuth access token has expired")
        if not access_token.value:
            raise CredentialUnavailable("OAuth access token is empty")

        return BearerCredential(
            token=access_token.value,
            expires_at=access_token.expires_at,
            base_url=self._base_url,
            source_identity="xai_oauth",
        )


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: str


class ProviderRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None,
        code: str | None,
        raw_body: str,
        headers: Mapping[str, str],
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.raw_body = raw_body
        self.headers = dict(headers)


class EntitlementDenied(ProviderRequestError):
    @classmethod
    def from_error(cls, error: ProviderRequestError) -> "EntitlementDenied":
        return cls(
            str(error),
            status=error.status,
            code=error.code,
            raw_body=error.raw_body,
            headers=error.headers,
        )


class UntrustedInferenceHost(ValueError):
    pass


class JsonHttpClient(Protocol):
    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> HttpResponse: ...


class UrllibJsonHttpClient:
    def __init__(self, timeout_seconds: float = 30) -> None:
        self._timeout_seconds = timeout_seconds

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> HttpResponse:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return HttpResponse(
                    status=response.status,
                    headers=dict(response.headers),
                    body=response.read().decode("utf-8", errors="replace"),
                )
        except HTTPError as error:
            return HttpResponse(
                status=error.code,
                headers=dict(error.headers or {}),
                body=error.read().decode("utf-8", errors="replace"),
            )
        except (URLError, TimeoutError, OSError) as error:
            raise ProviderRequestError(
                "xAI network request failed",
                status=None,
                code="network_error",
                raw_body="",
                headers={},
            ) from error


@dataclass(frozen=True, slots=True)
class XaiTextResponse:
    response_id: str | None
    model: str
    text: str
    credential_source: str
    fallback_from: str | None = None


class CredentialPolicy(StrEnum):
    OAUTH_ONLY = "oauth_only"
    API_KEY_ONLY = "api_key_only"
    OAUTH_THEN_API_KEY = "oauth_then_api_key"


class CredentialSource(Protocol):
    def get_credential(self) -> BearerCredential: ...


class ResponsesTransport(Protocol):
    def complete(
        self,
        credential: BearerCredential,
        model: str,
        prompt: str,
    ) -> XaiTextResponse: ...


class XaiResponsesTransport:
    def __init__(
        self,
        http_client: JsonHttpClient | None = None,
        *,
        allowed_hosts: frozenset[str] | None = None,
        entitlement_classifier: Callable[[ProviderRequestError], bool] | None = None,
    ) -> None:
        self._http_client = http_client or UrllibJsonHttpClient()
        self._allowed_hosts = allowed_hosts or frozenset({"api.x.ai"})
        self._entitlement_classifier = entitlement_classifier

    def complete(
        self,
        credential: BearerCredential,
        model: str,
        prompt: str,
    ) -> XaiTextResponse:
        endpoint = self._responses_endpoint(credential.base_url)
        response = self._http_client.post_json(
            endpoint,
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {credential.token}",
                "Content-Type": "application/json",
            },
            {"model": model, "input": prompt},
        )
        body = self._decode_json(response)
        if not 200 <= response.status < 300:
            error = self._request_error(response, body)
            if self._entitlement_classifier is not None and self._entitlement_classifier(error):
                raise EntitlementDenied.from_error(error)
            raise error

        text_parts = []
        found_output_text = False
        output = body.get("output", [])
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "message" or item.get("role") != "assistant":
                    continue
                content = item.get("content", [])
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict) or part.get("type") != "output_text":
                        continue
                    text = part.get("text")
                    if isinstance(text, str):
                        found_output_text = True
                        text_parts.append(text)

        if not found_output_text:
            raise ProviderRequestError(
                "xAI response contained no assistant output text",
                status=response.status,
                code="invalid_response",
                raw_body=response.body,
                headers=response.headers,
            )

        response_id = body.get("id")
        response_model = body.get("model")
        return XaiTextResponse(
            response_id=response_id if isinstance(response_id, str) else None,
            model=response_model if isinstance(response_model, str) else model,
            text="".join(text_parts),
            credential_source=credential.source_identity,
        )

    def _responses_endpoint(self, base_url: str) -> str:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self._allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise UntrustedInferenceHost("xAI credential target is not an allowed HTTPS host")
        return f"{base_url.rstrip('/')}/responses"

    @staticmethod
    def _decode_json(response: HttpResponse) -> dict[str, object]:
        try:
            body = json.loads(response.body)
        except json.JSONDecodeError as error:
            raise ProviderRequestError(
                "xAI returned invalid JSON",
                status=response.status,
                code="invalid_json",
                raw_body=response.body,
                headers=response.headers,
            ) from error
        if not isinstance(body, dict):
            raise ProviderRequestError(
                "xAI returned a non-object JSON response",
                status=response.status,
                code="invalid_json",
                raw_body=response.body,
                headers=response.headers,
            )
        return body

    @staticmethod
    def _request_error(
        response: HttpResponse,
        body: Mapping[str, object],
    ) -> ProviderRequestError:
        error_body = body.get("error")
        if isinstance(error_body, dict):
            code = error_body.get("code")
            message = error_body.get("message")
        else:
            code = None
            message = None
        return ProviderRequestError(
            message if isinstance(message, str) else f"xAI request failed with HTTP {response.status}",
            status=response.status,
            code=code if isinstance(code, str) else None,
            raw_body=response.body,
            headers=response.headers,
        )


class XaiProvider:
    def __init__(
        self,
        transport: ResponsesTransport,
        policy: CredentialPolicy,
        *,
        oauth_source: CredentialSource | None = None,
        api_key_source: CredentialSource | None = None,
        model: str = "grok-4.5",
    ) -> None:
        if policy in (CredentialPolicy.OAUTH_ONLY, CredentialPolicy.OAUTH_THEN_API_KEY):
            if oauth_source is None:
                raise ValueError(f"{policy.value} requires an OAuth credential source")
        if policy in (CredentialPolicy.API_KEY_ONLY, CredentialPolicy.OAUTH_THEN_API_KEY):
            if api_key_source is None:
                raise ValueError(f"{policy.value} requires an API-key credential source")

        self._transport = transport
        self._policy = policy
        self._oauth_source = oauth_source
        self._api_key_source = api_key_source
        self._model = model

    def respond(self, prompt: str) -> XaiTextResponse:
        if self._policy is CredentialPolicy.API_KEY_ONLY:
            return self._complete(self._api_key_source, prompt)
        if self._policy is CredentialPolicy.OAUTH_ONLY:
            return self._complete(self._oauth_source, prompt)

        fallback_from = "xai_oauth"
        try:
            oauth_credential = self._oauth_source.get_credential()
            fallback_from = oauth_credential.source_identity
            return self._transport.complete(oauth_credential, self._model, prompt)
        except EntitlementDenied:
            api_key_result = self._complete(self._api_key_source, prompt)
            return replace(
                api_key_result,
                fallback_from=fallback_from,
            )

    def _complete(
        self,
        source: CredentialSource | None,
        prompt: str,
    ) -> XaiTextResponse:
        if source is None:
            raise CredentialUnavailable("credential source is not configured")
        credential = source.get_credential()
        return self._transport.complete(credential, self._model, prompt)
