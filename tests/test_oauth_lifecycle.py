from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from threading import Lock
from urllib.parse import parse_qs

import pytest

from lenkobot.xai_provider import (
    EntitlementDenied,
    HttpResponse,
    OAuthCredentialSource,
    OAuthRefreshCoordinator,
    OAuthTokenState,
    ProviderRequestError,
    XaiOAuthRefreshClient,
    UrllibJsonHttpClient,
)


class MemoryCredentialStore:
    def __init__(self, state):
        self.state = state
        self.saved = []

    def load(self):
        return self.state

    def save(self, state):
        self.state = state
        self.saved.append(state)


class RecordingRefreshClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def refresh(self, state):
        self.calls.append(state)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class RecordingFormHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post_form(self, url, headers, payload):
        self.calls.append((url, headers, payload))
        return self.response


def token_state(now, *, access="access-secret", refresh="refresh-secret"):
    return OAuthTokenState(
        access_token=access,
        refresh_token=refresh,
        expires_at=now + timedelta(hours=1),
    )


def source(store, refresh_client, now, *, lock=None):
    coordinator = OAuthRefreshCoordinator(
        store,
        refresh_client,
        lock=lock or Lock(),
        now=lambda: now,
        refresh_skew=timedelta(seconds=60),
    )
    return OAuthCredentialSource(
        coordinator,
        base_url="https://api.x.ai/v1",
    )


def test_valid_access_token_is_returned_without_refresh_or_persist(tmp_path):
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    store = MemoryCredentialStore(token_state(now))
    refresher = RecordingRefreshClient(token_state(now, access="new-access"))

    credential = source(store, refresher, now).get_credential()

    assert credential.token == "access-secret"
    assert credential.expires_at == now + timedelta(hours=1)
    assert refresher.calls == []
    assert store.saved == []
    assert "access-secret" not in repr(credential)


def test_expired_access_token_is_refreshed_and_persisted():
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    expired = OAuthTokenState(
        access_token="old-access",
        refresh_token="refresh-secret",
        expires_at=now - timedelta(seconds=1),
    )
    refreshed = token_state(now, access="new-access", refresh="rotated-refresh")
    store = MemoryCredentialStore(expired)
    refresher = RecordingRefreshClient(refreshed)

    credential = source(store, refresher, now).get_credential()

    assert credential.token == "new-access"
    assert store.saved == [refreshed]
    assert refresher.calls == [expired]
    assert "rotated-refresh" not in repr(credential)


def test_concurrent_expired_reads_refresh_once_after_lock_recheck():
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    expired = OAuthTokenState(
        access_token="old-access",
        refresh_token="refresh-secret",
        expires_at=now - timedelta(seconds=1),
    )
    refreshed = token_state(now, access="new-access", refresh="rotated-refresh")
    store = MemoryCredentialStore(expired)
    refresher = RecordingRefreshClient(refreshed)
    lock = Lock()
    sources = (
        source(store, refresher, now, lock=lock),
        source(store, refresher, now, lock=lock),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        credentials = tuple(
            executor.map(lambda item: item.get_credential(), sources)
        )

    assert [credential.token for credential in credentials] == [
        "new-access",
        "new-access",
    ]
    assert len(refresher.calls) == 1
    assert store.saved == [refreshed]


def test_refresh_failure_preserves_state_and_does_not_expose_secret():
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    expired = OAuthTokenState(
        access_token="old-access",
        refresh_token="refresh-secret",
        expires_at=now - timedelta(seconds=1),
    )
    store = MemoryCredentialStore(expired)
    refresher = RecordingRefreshClient(
        ProviderRequestError(
            "OAuth refresh failed",
            status=503,
            code="temporarily_unavailable",
            raw_body="",
            headers={},
        )
    )

    with pytest.raises(ProviderRequestError) as error:
        source(store, refresher, now).get_credential()

    assert store.state == expired
    assert store.saved == []
    assert "old-access" not in repr(error.value)
    assert "refresh-secret" not in repr(error.value)


def test_refresh_client_posts_form_and_keeps_rotated_refresh_token():
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    http_client = RecordingFormHttpClient(
        HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "access_token": "new-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            ),
        )
    )
    client = XaiOAuthRefreshClient(
        client_id="public-client-id",
        http_client=http_client,
        now=lambda: now,
    )
    state = OAuthTokenState(
        access_token="old-access",
        refresh_token="refresh-secret",
        expires_at=now - timedelta(seconds=1),
    )

    refreshed = client.refresh(state)

    assert refreshed == OAuthTokenState(
        access_token="new-access",
        refresh_token="rotated-refresh",
        expires_at=now + timedelta(seconds=3600),
    )
    assert http_client.calls == [
        (
            "https://auth.x.ai/oauth2/token",
            {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            {
                "grant_type": "refresh_token",
                "refresh_token": "refresh-secret",
                "client_id": "public-client-id",
            },
        )
    ]


def test_refresh_client_does_not_expose_secret_in_http_error():
    http_client = RecordingFormHttpClient(
        HttpResponse(
            status=400,
            headers={},
            body=json.dumps(
                {
                    "error": "invalid_grant",
                    "error_description": "refresh-secret was rejected",
                }
            ),
        )
    )
    client = XaiOAuthRefreshClient(
        client_id="public-client-id",
        http_client=http_client,
    )
    state = OAuthTokenState(
        access_token="old-access",
        refresh_token="refresh-secret",
        expires_at=datetime.now(timezone.utc),
    )

    with pytest.raises(ProviderRequestError) as error:
        client.refresh(state)

    assert not isinstance(error.value, EntitlementDenied)
    assert error.value.code == "invalid_grant"
    assert "refresh-secret" not in repr(error.value)


def test_refresh_client_uses_typed_entitlement_classifier():
    http_client = RecordingFormHttpClient(
        HttpResponse(
            status=403,
            headers={},
            body=json.dumps(
                {"error": "account_not_entitled", "error_description": "denied"}
            ),
        )
    )
    client = XaiOAuthRefreshClient(
        client_id="public-client-id",
        http_client=http_client,
        entitlement_classifier=lambda error: error.code == "account_not_entitled",
    )
    state = OAuthTokenState(
        access_token="old-access",
        refresh_token="refresh-secret",
        expires_at=datetime.now(timezone.utc),
    )

    with pytest.raises(EntitlementDenied):
        client.refresh(state)


def test_urllib_form_client_encodes_payload_and_preserves_timeout(monkeypatch):
    observed = {}

    class UrlResponse:
        status = 200
        headers = {"x-request-id": "request-1"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        observed["request"] = request
        observed["timeout"] = timeout
        return UrlResponse()

    monkeypatch.setattr("lenkobot.xai_provider.urlopen", fake_urlopen)
    response = UrllibJsonHttpClient(timeout_seconds=12).post_form(
        "https://auth.x.ai/oauth2/token",
        {"Accept": "application/json"},
        {"refresh_token": "refresh secret", "client_id": "client-1"},
    )

    assert response == HttpResponse(
        status=200,
        headers={"x-request-id": "request-1"},
        body='{"ok": true}',
    )
    assert observed["request"].full_url == "https://auth.x.ai/oauth2/token"
    assert parse_qs(observed["request"].data.decode("utf-8")) == {
        "refresh_token": ["refresh secret"],
        "client_id": ["client-1"],
    }
    assert observed["timeout"] == 12


@pytest.mark.parametrize(
    "token_url",
    (
        "http://auth.x.ai/oauth2/token",
        "https://auth.x.ai.attacker.test/oauth2/token",
        "https://auth.x.ai:8443/oauth2/token",
        "https://auth.x.ai/oauth2/token?redirect=evil",
    ),
)
def test_refresh_client_rejects_untrusted_token_endpoint(token_url):
    with pytest.raises(ValueError):
        XaiOAuthRefreshClient(client_id="public-client-id", token_url=token_url)


def test_refresh_coordinator_requires_all_lifecycle_dependencies():
    with pytest.raises(ValueError):
        OAuthRefreshCoordinator(None, object(), lock=Lock())
    with pytest.raises(ValueError):
        OAuthRefreshCoordinator(object(), None, lock=Lock())
    with pytest.raises(ValueError):
        OAuthRefreshCoordinator(object(), object(), lock=None)
    with pytest.raises(ValueError):
        OAuthCredentialSource(None, base_url="https://api.x.ai/v1")
