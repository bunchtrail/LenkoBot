from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
from typing import Protocol, TypeAlias
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
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


@dataclass(frozen=True, slots=True)
class OAuthTokenState:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
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


class OAuthCredentialStore(Protocol):
    def load(self) -> OAuthTokenState | None: ...

    def save(self, state: OAuthTokenState) -> None: ...


class OAuthRefreshClient(Protocol):
    def refresh(self, state: OAuthTokenState) -> OAuthTokenState: ...


class OAuthRefreshLock(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, exc_type, exc_value, traceback) -> bool | None: ...


class OAuthRefreshCoordinator:
    def __init__(
        self,
        store: OAuthCredentialStore,
        refresh_client: OAuthRefreshClient,
        *,
        lock: OAuthRefreshLock,
        now: Callable[[], datetime] | None = None,
        refresh_skew: timedelta = timedelta(seconds=60),
    ) -> None:
        if store is None or refresh_client is None or lock is None:
            raise ValueError("OAuth refresh dependencies are required")
        if refresh_skew.total_seconds() < 0:
            raise ValueError("OAuth refresh skew cannot be negative")

        self._store = store
        self._refresh_client = refresh_client
        self._lock = lock
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._refresh_skew = refresh_skew

    def get_access_token(self) -> OAuthAccessToken:
        with self._lock:
            state = self._load_state()
            now = self._now()
            self._validate_clock(now)
            self._validate_state(state)
            if state.expires_at > now + self._refresh_skew:
                return self._as_access_token(state)
            if not state.refresh_token.strip():
                raise CredentialUnavailable("OAuth refresh token is unavailable")

            try:
                refreshed = self._refresh_client.refresh(state)
            except (CredentialUnavailable, ProviderRequestError):
                raise
            except Exception:
                raise CredentialUnavailable("OAuth token refresh failed") from None

            self._validate_state(refreshed)
            if refreshed.expires_at <= now:
                raise CredentialUnavailable("OAuth refresh returned an expired token")
            self._save_state(refreshed)
            return self._as_access_token(refreshed)

    def _load_state(self) -> OAuthTokenState:
        try:
            state = self._store.load()
        except Exception:
            raise CredentialUnavailable("OAuth credential store could not load state") from None
        if state is None:
            raise CredentialUnavailable("OAuth credential state is unavailable")
        return state

    def _save_state(self, state: OAuthTokenState) -> None:
        try:
            self._store.save(state)
        except Exception:
            raise CredentialUnavailable("OAuth credential state could not be saved") from None

    @staticmethod
    def _validate_clock(now: datetime) -> None:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise CredentialUnavailable("OAuth clock must include a timezone")

    @staticmethod
    def _validate_state(state: OAuthTokenState) -> None:
        if not isinstance(state, OAuthTokenState):
            raise CredentialUnavailable("OAuth credential state is invalid")
        if not isinstance(state.access_token, str) or not state.access_token.strip():
            raise CredentialUnavailable("OAuth access token is empty")
        if not isinstance(state.refresh_token, str):
            raise CredentialUnavailable("OAuth refresh token is invalid")
        if not isinstance(state.expires_at, datetime) or state.expires_at.tzinfo is None:
            raise CredentialUnavailable("OAuth token expiry must include a timezone")

    @staticmethod
    def _as_access_token(state: OAuthTokenState) -> OAuthAccessToken:
        return OAuthAccessToken(value=state.access_token, expires_at=state.expires_at)


class OAuthCredentialSource:
    def __init__(
        self,
        coordinator: OAuthRefreshCoordinator,
        *,
        base_url: str,
    ) -> None:
        if coordinator is None:
            raise ValueError("OAuth refresh coordinator is required")
        if not base_url.strip():
            raise ValueError("OAuth inference base URL cannot be empty")

        self._coordinator = coordinator
        self._base_url = base_url

    def get_credential(self) -> BearerCredential:
        access_token = self._coordinator.get_access_token()

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


@dataclass(frozen=True, slots=True)
class XaiInputMessage:
    role: str
    content: str


XaiPrompt: TypeAlias = str | tuple[XaiInputMessage, ...]


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

    def post_form(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, str],
    ) -> HttpResponse:
        request_headers = dict(headers)
        request_headers.setdefault(
            "Content-Type", "application/x-www-form-urlencoded"
        )
        request = Request(
            url,
            data=urlencode(payload).encode("utf-8"),
            headers=request_headers,
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
                "xAI OAuth token request failed",
                status=None,
                code="network_error",
                raw_body="",
                headers={},
            ) from error


class FormHttpClient(Protocol):
    def post_form(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, str],
    ) -> HttpResponse: ...


class XaiOAuthRefreshClient:
    def __init__(
        self,
        *,
        client_id: str,
        http_client: FormHttpClient | None = None,
        token_url: str = "https://auth.x.ai/oauth2/token",
        now: Callable[[], datetime] | None = None,
        entitlement_classifier: Callable[[ProviderRequestError], bool] | None = None,
    ) -> None:
        if not client_id.strip():
            raise ValueError("OAuth client ID cannot be empty")
        self._validate_token_url(token_url)
        self._client_id = client_id
        self._http_client = http_client or UrllibJsonHttpClient()
        self._token_url = token_url
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._entitlement_classifier = entitlement_classifier

    def refresh(self, state: OAuthTokenState) -> OAuthTokenState:
        if not state.refresh_token.strip():
            raise CredentialUnavailable("OAuth refresh token is unavailable")
        response = self._http_client.post_form(
            self._token_url,
            {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            {
                "grant_type": "refresh_token",
                "refresh_token": state.refresh_token,
                "client_id": self._client_id,
            },
        )
        body = self._decode_response(response)
        if not 200 <= response.status < 300:
            error = self._request_error(response, body)
            if (
                self._entitlement_classifier is not None
                and self._entitlement_classifier(error)
            ):
                raise EntitlementDenied.from_error(error)
            raise error

        access_token = body.get("access_token")
        refresh_token = body.get("refresh_token", state.refresh_token)
        expires_in = body.get("expires_in")
        if not isinstance(access_token, str) or not access_token.strip():
            raise CredentialUnavailable("OAuth refresh returned no access token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            raise CredentialUnavailable("OAuth refresh returned no refresh token")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or expires_in <= 0
        ):
            raise CredentialUnavailable("OAuth refresh returned invalid expiry")
        now = self._now()
        if now.tzinfo is None:
            raise CredentialUnavailable("OAuth clock must include a timezone")
        return OAuthTokenState(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=now + timedelta(seconds=float(expires_in)),
        )

    @staticmethod
    def _validate_token_url(token_url: str) -> None:
        try:
            parsed = urlsplit(token_url)
            port = parsed.port
        except ValueError:
            raise ValueError("OAuth token target is not an allowed HTTPS host") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != "auth.x.ai"
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("OAuth token target is not an allowed HTTPS host")

    @staticmethod
    def _decode_response(response: HttpResponse) -> dict[str, object]:
        try:
            body = json.loads(response.body)
        except json.JSONDecodeError as error:
            raise ProviderRequestError(
                "xAI OAuth token endpoint returned invalid JSON",
                status=response.status,
                code="invalid_json",
                raw_body="",
                headers={},
            ) from error
        if not isinstance(body, dict):
            raise ProviderRequestError(
                "xAI OAuth token endpoint returned an invalid response",
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
            "xAI OAuth token refresh failed",
            status=response.status,
            code=error_code if isinstance(error_code, str) else None,
            raw_body="",
            headers={},
        )


@dataclass(frozen=True, slots=True)
class XaiTextResponse:
    response_id: str | None
    model: str
    text: str
    credential_source: str
    fallback_from: str | None = None


@dataclass(frozen=True, slots=True)
class XaiFunctionCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class XaiToolOutput:
    call_id: str
    output: str


@dataclass(frozen=True, slots=True)
class XaiToolTurn:
    response_id: str | None
    model: str
    text: str | None
    tool_calls: tuple[XaiFunctionCall, ...]
    credential_source: str
    fallback_from: str | None = None


@dataclass(frozen=True, slots=True)
class XaiStructuredResponse:
    response_id: str | None
    model: str
    value: object
    credential_source: str


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
        prompt: XaiPrompt,
    ) -> XaiTextResponse: ...


class StructuredResponsesTransport(Protocol):
    def complete_structured(
        self,
        credential: BearerCredential,
        model: str,
        prompt: str,
        *,
        schema_name: str,
        schema: dict[str, object],
    ) -> XaiStructuredResponse: ...


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
        prompt: XaiPrompt,
    ) -> XaiTextResponse:
        response, body = self._post_response(
            credential,
            model,
            prompt,
        )
        text = self._output_text(body, response)
        response_id = body.get("id")
        response_model = body.get("model")
        return XaiTextResponse(
            response_id=response_id if isinstance(response_id, str) else None,
            model=response_model if isinstance(response_model, str) else model,
            text=text,
            credential_source=credential.source_identity,
        )

    def complete_structured(
        self,
        credential: BearerCredential,
        model: str,
        prompt: str,
        *,
        schema_name: str,
        schema: dict[str, object],
    ) -> XaiStructuredResponse:
        if not schema_name or not isinstance(schema, dict):
            raise ValueError("structured response schema is invalid")
        response, body = self._post_response(
            credential,
            model,
            prompt,
            response_format={
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            },
        )
        text = self._output_text(body, response)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise ProviderRequestError(
                "xAI structured response was not valid JSON",
                status=response.status,
                code="invalid_structured_response",
                raw_body=response.body,
                headers=response.headers,
            ) from error
        response_id = body.get("id")
        response_model = body.get("model")
        return XaiStructuredResponse(
            response_id=response_id if isinstance(response_id, str) else None,
            model=response_model if isinstance(response_model, str) else model,
            value=value,
            credential_source=credential.source_identity,
        )

    def complete_with_tools(
        self,
        credential: BearerCredential,
        model: str,
        prompt: XaiPrompt,
        *,
        tools: tuple[dict[str, object], ...],
    ) -> XaiToolTurn:
        validated_tools = _validate_tools(tools)
        response, body = self._post_response(
            credential,
            model,
            prompt,
            tools=validated_tools,
        )
        return self._tool_turn(body, response, credential, model=model)

    def complete_with_tool_output(
        self,
        credential: BearerCredential,
        model: str,
        *,
        previous_response_id: str,
        tool_outputs: tuple[XaiToolOutput, ...],
        tools: tuple[dict[str, object], ...],
    ) -> XaiToolTurn:
        validated_tools = _validate_tools(tools)
        if not isinstance(previous_response_id, str) or not previous_response_id.strip():
            raise ValueError("previous response ID cannot be empty")
        if not tool_outputs:
            raise ValueError("tool outputs cannot be empty")
        for tool_output in tool_outputs:
            if not isinstance(tool_output, XaiToolOutput):
                raise ValueError("tool output is invalid")
            if (
                not isinstance(tool_output.call_id, str)
                or not tool_output.call_id.strip()
                or not isinstance(tool_output.output, str)
            ):
                raise ValueError("tool output is invalid")
        response, body = self._post_response(
            credential,
            model,
            "",
            tools=validated_tools,
            previous_response_id=previous_response_id.strip(),
            tool_outputs=tool_outputs,
        )
        return self._tool_turn(body, response, credential, model=model)

    def _tool_turn(
        self,
        body: Mapping[str, object],
        response: HttpResponse,
        credential: BearerCredential,
        *,
        model: str,
    ) -> XaiToolTurn:
        text = self._output_text_or_none(body)
        calls = []
        output = body.get("output", [])
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "function_call":
                    continue
                call_id = item.get("call_id")
                name = item.get("name")
                arguments = item.get("arguments")
                if not isinstance(call_id, str) or not call_id.strip():
                    continue
                if not isinstance(name, str) or not name.strip():
                    continue
                if not isinstance(arguments, str):
                    continue
                calls.append(
                    XaiFunctionCall(
                        call_id=call_id,
                        name=name,
                        arguments=arguments,
                    )
                )
        if text is None and not calls:
            raise ProviderRequestError(
                "xAI response contained no assistant output",
                status=response.status,
                code="invalid_response",
                raw_body=response.body,
                headers=response.headers,
            )
        response_id = body.get("id")
        response_model = body.get("model")
        return XaiToolTurn(
            response_id=response_id if isinstance(response_id, str) else None,
            model=response_model if isinstance(response_model, str) else model,
            text=text,
            tool_calls=tuple(calls),
            credential_source=credential.source_identity,
        )

    def _post_response(
        self,
        credential: BearerCredential,
        model: str,
        prompt: XaiPrompt,
        *,
        response_format: dict[str, object] | None = None,
        tools: tuple[dict[str, object], ...] | None = None,
        previous_response_id: str | None = None,
        tool_outputs: tuple[XaiToolOutput, ...] | None = None,
    ) -> tuple[HttpResponse, dict[str, object]]:
        endpoint = self._responses_endpoint(credential.base_url)
        if tool_outputs is not None:
            request_input: object = [
                {
                    "type": "function_call_output",
                    "call_id": tool_output.call_id,
                    "output": tool_output.output,
                }
                for tool_output in tool_outputs
            ]
        else:
            request_input = _serialize_input(prompt)
        payload: dict[str, object] = {
            "model": model,
            "input": request_input,
        }
        if response_format is not None:
            payload["text"] = {"format": response_format}
        if tools is not None:
            payload["tools"] = [dict(tool) for tool in tools]
            payload["parallel_tool_calls"] = False
        if previous_response_id is not None:
            payload["previous_response_id"] = previous_response_id
        response = self._http_client.post_json(
            endpoint,
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {credential.token}",
                "Content-Type": "application/json",
            },
            payload,
        )
        body = self._decode_json(response)
        if not 200 <= response.status < 300:
            error = self._request_error(response, body)
            if self._entitlement_classifier is not None and self._entitlement_classifier(error):
                raise EntitlementDenied.from_error(error)
            raise error
        return response, body

    @staticmethod
    def _output_text_or_none(body: Mapping[str, object]) -> str | None:
        text_parts = []
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
                        text_parts.append(text)
        if not text_parts:
            return None
        return "".join(text_parts)

    @classmethod
    def _output_text(cls, body: Mapping[str, object], response: HttpResponse) -> str:
        text = cls._output_text_or_none(body)
        if text is None:
            raise ProviderRequestError(
                "xAI response contained no assistant output text",
                status=response.status,
                code="invalid_response",
                raw_body=response.body,
                headers=response.headers,
            )
        return text

    def _responses_endpoint(self, base_url: str) -> str:
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except ValueError:
            raise UntrustedInferenceHost(
                "xAI credential target is not an allowed HTTPS host"
            ) from None
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self._allowed_hosts
            or port not in (None, 443)
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
    supports_message_input = True
    supports_tools = True

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

    def respond(self, prompt: XaiPrompt) -> XaiTextResponse:
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
        prompt: XaiPrompt,
    ) -> XaiTextResponse:
        if source is None:
            raise CredentialUnavailable("credential source is not configured")
        credential = source.get_credential()
        return self._transport.complete(credential, self._model, prompt)

    def respond_with_tools(
        self,
        prompt: XaiPrompt,
        *,
        tools: tuple[dict[str, object], ...],
    ) -> XaiToolTurn:
        return self._tool_turn(
            lambda credential: self._transport.complete_with_tools(
                credential,
                self._model,
                prompt,
                tools=tools,
            )
        )

    def respond_with_tool_outputs(
        self,
        *,
        previous_response_id: str,
        tool_outputs: tuple[XaiToolOutput, ...],
        tools: tuple[dict[str, object], ...],
    ) -> XaiToolTurn:
        return self._tool_turn(
            lambda credential: self._transport.complete_with_tool_output(
                credential,
                self._model,
                previous_response_id=previous_response_id,
                tool_outputs=tool_outputs,
                tools=tools,
            )
        )

    def _tool_turn(
        self,
        invoke: Callable[[BearerCredential], XaiToolTurn],
    ) -> XaiToolTurn:
        if self._policy is CredentialPolicy.API_KEY_ONLY:
            return self._complete_tool_turn(self._api_key_source, invoke)
        if self._policy is CredentialPolicy.OAUTH_ONLY:
            return self._complete_tool_turn(self._oauth_source, invoke)

        fallback_from = "xai_oauth"
        try:
            oauth_credential = self._oauth_source.get_credential()
            fallback_from = oauth_credential.source_identity
            return invoke(oauth_credential)
        except EntitlementDenied:
            result = self._complete_tool_turn(self._api_key_source, invoke)
            return replace(result, fallback_from=fallback_from)

    @staticmethod
    def _complete_tool_turn(
        source: CredentialSource | None,
        invoke: Callable[[BearerCredential], XaiToolTurn],
    ) -> XaiToolTurn:
        if source is None:
            raise CredentialUnavailable("credential source is not configured")
        return invoke(source.get_credential())


class XaiStructuredProvider:
    def __init__(
        self,
        transport: StructuredResponsesTransport,
        *,
        oauth_source: CredentialSource,
        model: str = "grok-4.5",
    ) -> None:
        self._transport = transport
        self._oauth_source = oauth_source
        self._model = model

    def respond(
        self,
        prompt: str,
        *,
        schema_name: str,
        schema: dict[str, object],
    ) -> XaiStructuredResponse:
        credential = self._oauth_source.get_credential()
        return self._transport.complete_structured(
            credential,
            self._model,
            prompt,
            schema_name=schema_name,
            schema=schema,
        )


def _validate_tools(
    tools: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    if isinstance(tools, list):
        tools = tuple(tools)
    if not isinstance(tools, tuple) or not tools:
        raise ValueError("xAI tools must be a non-empty tuple")
    validated = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise ValueError("xAI tool definition is invalid")
        if tool.get("type") != "function":
            raise ValueError("xAI tool type must be function")
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("xAI tool name cannot be empty")
        parameters = tool.get("parameters")
        if not isinstance(parameters, Mapping):
            raise ValueError("xAI tool parameters must be an object")
        validated.append(dict(tool))
    return tuple(validated)


def _serialize_input(prompt: XaiPrompt) -> str | list[dict[str, str]]:
    if isinstance(prompt, str):
        return prompt
    if not isinstance(prompt, tuple) or not prompt:
        raise ValueError("xAI input messages must be a non-empty tuple")
    serialized: list[dict[str, str]] = []
    for message in prompt:
        if not isinstance(message, XaiInputMessage):
            raise ValueError("xAI input contains an invalid message")
        if message.role not in {"system", "user", "assistant"}:
            raise ValueError("xAI input contains an invalid role")
        if not message.content.strip():
            raise ValueError("xAI input message content cannot be empty")
        serialized.append({"role": message.role, "content": message.content})
    return serialized
