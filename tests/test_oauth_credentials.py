from datetime import datetime, timedelta, timezone
import json

import pytest

from lenkobot.oauth_credentials import (
    MutexWaitResult,
    OAuthDeviceAuthorization,
    WindowsOAuthCredentialStore,
    WindowsOAuthRefreshMutex,
    XaiOAuthDeviceClient,
)
from lenkobot.xai_provider import (
    CredentialUnavailable,
    HttpResponse,
    OAuthTokenState,
)


class FakeCredentialApi:
    def __init__(self, blob=None):
        self.blob = blob
        self.reads = []
        self.writes = []

    def read(self, target_name):
        self.reads.append(target_name)
        return self.blob

    def write(self, target_name, blob):
        self.writes.append((target_name, blob))
        self.blob = blob


class FakeMutexApi:
    def __init__(self, wait_result):
        self.wait_result = wait_result
        self.calls = []
        self.handle = object()

    def create(self, name):
        self.calls.append(("create", name))
        return self.handle

    def wait(self, handle, timeout_ms):
        self.calls.append(("wait", handle, timeout_ms))
        return self.wait_result

    def release(self, handle):
        self.calls.append(("release", handle))

    def close(self, handle):
        self.calls.append(("close", handle))


class RecordingFormHttpClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post_form(self, url, headers, payload):
        self.calls.append((url, headers, payload))
        return self.responses.pop(0)


class AdvancingClock:
    def __init__(self, now):
        self.current = now
        self.sleeps = []

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


class RecordingLock:
    def __init__(self):
        self.events = []

    def __enter__(self):
        self.events.append("enter")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.events.append("exit")


def state(now, *, access="access-secret", refresh="refresh-secret"):
    return OAuthTokenState(
        access_token=access,
        refresh_token=refresh,
        expires_at=now + timedelta(hours=1),
    )


def response(status, body):
    return HttpResponse(status=status, headers={}, body=json.dumps(body))


def test_windows_credential_store_round_trips_versioned_state():
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    api = FakeCredentialApi()
    store = WindowsOAuthCredentialStore(profile_id="default", api=api)
    expected = state(now)

    store.save(expected)
    loaded = store.load()

    assert loaded == expected
    assert api.reads == ["LenkoBot/xai-oauth/v1/default"]
    assert api.writes[0][0] == "LenkoBot/xai-oauth/v1/default"
    assert "access-secret" not in repr(loaded)
    assert "refresh-secret" not in repr(loaded)


def test_windows_credential_store_returns_none_for_missing_record():
    store = WindowsOAuthCredentialStore(
        profile_id="default",
        api=FakeCredentialApi(),
    )

    assert store.load() is None


def test_windows_credential_store_rejects_malformed_or_oversized_state():
    malformed = FakeCredentialApi(
        b'{"access_token":"access-secret","refresh_token":"refresh-secret"}'
    )
    store = WindowsOAuthCredentialStore(profile_id="default", api=malformed)

    with pytest.raises(CredentialUnavailable) as error:
        store.load()

    assert "access-secret" not in repr(error.value)
    assert "refresh-secret" not in repr(error.value)

    oversized_api = FakeCredentialApi()
    oversized_store = WindowsOAuthCredentialStore(
        profile_id="default",
        api=oversized_api,
    )
    with pytest.raises(CredentialUnavailable, match="too large"):
        oversized_store.save(
            OAuthTokenState(
                access_token="a" * 2600,
                refresh_token="refresh-secret",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )

    assert oversized_api.writes == []


@pytest.mark.parametrize(
    "wait_result",
    (MutexWaitResult.ACQUIRED, MutexWaitResult.ABANDONED),
)
def test_windows_refresh_mutex_releases_acquired_or_abandoned_lock(wait_result):
    api = FakeMutexApi(wait_result)
    mutex = WindowsOAuthRefreshMutex(
        "LenkoBot/xai-oauth/v1/default",
        api=api,
        timeout_seconds=10,
    )

    with mutex:
        pass

    assert api.calls[0][0] == "create"
    assert api.calls[0][1].startswith("Local\\LenkoBot.XaiOAuth.Refresh.")
    assert api.calls[1] == ("wait", api.handle, 10_000)
    assert api.calls[-2:] == [("release", api.handle), ("close", api.handle)]


def test_windows_refresh_mutex_timeout_closes_without_release():
    api = FakeMutexApi(MutexWaitResult.TIMEOUT)
    mutex = WindowsOAuthRefreshMutex(
        "LenkoBot/xai-oauth/v1/default",
        api=api,
        timeout_seconds=1,
    )

    with pytest.raises(CredentialUnavailable, match="timed out"):
        with mutex:
            pass

    assert ("release", api.handle) not in api.calls
    assert api.calls[-1] == ("close", api.handle)


@pytest.mark.parametrize("timeout_seconds", (True, float("nan"), float("inf")))
def test_windows_refresh_mutex_rejects_invalid_timeout(timeout_seconds):
    with pytest.raises(ValueError, match="positive"):
        WindowsOAuthRefreshMutex(
            "LenkoBot/xai-oauth/v1/default",
            api=FakeMutexApi(MutexWaitResult.ACQUIRED),
            timeout_seconds=timeout_seconds,
        )


def test_device_start_returns_presentation_data_and_sends_exact_form():
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    http_client = RecordingFormHttpClient(
        response(
            200,
            {
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://accounts.x.ai/activate",
                "verification_uri_complete": "https://accounts.x.ai/activate?code=ABCD-EFGH",
                "expires_in": 900,
            },
        )
    )
    client = XaiOAuthDeviceClient(
        client_id="public-client-id",
        scopes=("openid", "offline_access"),
        http_client=http_client,
        now=lambda: now,
    )

    authorization = client.start_device_authorization()

    assert authorization.user_code == "ABCD-EFGH"
    assert authorization.verification_uri == "https://accounts.x.ai/activate"
    assert authorization.interval_seconds == 5
    assert authorization.expires_at == now + timedelta(seconds=900)
    assert "device-secret" not in repr(authorization)
    assert http_client.calls == [
        (
            "https://auth.x.ai/oauth2/device/code",
            {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            {
                "client_id": "public-client-id",
                "scope": "openid offline_access",
            },
        )
    ]


@pytest.mark.parametrize(
    ("device_url", "token_url"),
    (
        (
            "https://auth.x.ai:8443/oauth2/device/code",
            "https://auth.x.ai/oauth2/token",
        ),
        (
            "https://auth.x.ai/oauth2/device/code",
            "https://auth.x.ai:8443/oauth2/token",
        ),
    ),
)
def test_device_client_rejects_nonstandard_endpoint_port(device_url, token_url):
    with pytest.raises(ValueError, match="allowed HTTPS host"):
        XaiOAuthDeviceClient(
            client_id="public-client-id",
            device_url=device_url,
            token_url=token_url,
        )


@pytest.mark.parametrize("duration", (float("nan"), float("inf")))
def test_device_start_rejects_nonfinite_duration(duration):
    http_client = RecordingFormHttpClient(
        response(
            200,
            {
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://accounts.x.ai/activate",
                "expires_in": duration,
            },
        )
    )
    client = XaiOAuthDeviceClient(
        client_id="public-client-id",
        http_client=http_client,
    )

    with pytest.raises(CredentialUnavailable, match="invalid duration"):
        client.start_device_authorization()


def test_device_start_converts_untrusted_verification_uri_to_controlled_error():
    http_client = RecordingFormHttpClient(
        response(
            200,
            {
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://invalid.example/activate",
                "expires_in": 900,
            },
        )
    )
    client = XaiOAuthDeviceClient(
        client_id="public-client-id",
        http_client=http_client,
    )

    with pytest.raises(CredentialUnavailable, match="invalid verification URI") as error:
        client.start_device_authorization()

    assert "invalid.example" not in repr(error.value)


def test_device_complete_handles_pending_slow_down_and_persists_once():
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    clock = AdvancingClock(now)
    http_client = RecordingFormHttpClient(
        response(400, {"error": "authorization_pending"}),
        response(400, {"error": "slow_down"}),
        response(
            200,
            {
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
            },
        ),
    )
    client = XaiOAuthDeviceClient(
        client_id="public-client-id",
        http_client=http_client,
        now=clock.now,
        sleep=clock.sleep,
    )
    authorization = OAuthDeviceAuthorization(
        device_code="device-secret",
        user_code="ABCD-EFGH",
        verification_uri="https://auth.x.ai/activate",
        verification_uri_complete=None,
        expires_at=now + timedelta(minutes=5),
        interval_seconds=5,
    )
    store = FakeCredentialApi()
    credential_store = WindowsOAuthCredentialStore(profile_id="default", api=store)
    lock = RecordingLock()

    result = client.complete_device_authorization(
        authorization,
        store=credential_store,
        lock=lock,
    )

    assert result == OAuthTokenState(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=now + timedelta(seconds=20 + 3600),
    )
    assert clock.sleeps == [5, 5, 10]
    assert len(store.writes) == 1
    assert lock.events == ["enter", "exit"]
    assert [call[2]["grant_type"] for call in http_client.calls] == [
        "urn:ietf:params:oauth:grant-type:device_code",
        "urn:ietf:params:oauth:grant-type:device_code",
        "urn:ietf:params:oauth:grant-type:device_code",
    ]


@pytest.mark.parametrize(
    "device_code,interval_seconds",
    ((None, 5), ("device-secret", float("nan"))),
)
def test_device_complete_fails_closed_for_invalid_authorization(
    device_code,
    interval_seconds,
):
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    http_client = RecordingFormHttpClient()
    client = XaiOAuthDeviceClient(
        client_id="public-client-id",
        http_client=http_client,
        now=lambda: now,
        sleep=lambda seconds: None,
    )
    authorization = OAuthDeviceAuthorization(
        device_code=device_code,
        user_code="ABCD-EFGH",
        verification_uri="https://auth.x.ai/activate",
        verification_uri_complete=None,
        expires_at=now + timedelta(minutes=5),
        interval_seconds=interval_seconds,
    )
    api = FakeCredentialApi()

    with pytest.raises(CredentialUnavailable):
        client.complete_device_authorization(
            authorization,
            store=WindowsOAuthCredentialStore(profile_id="default", api=api),
            lock=RecordingLock(),
        )

    assert http_client.calls == []
    assert api.writes == []


def test_device_denial_or_missing_refresh_token_never_persists_state():
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    authorization = OAuthDeviceAuthorization(
        device_code="device-secret",
        user_code="ABCD-EFGH",
        verification_uri="https://auth.x.ai/activate",
        verification_uri_complete=None,
        expires_at=now + timedelta(minutes=5),
        interval_seconds=5,
    )

    for token_response in (
        response(400, {"error": "access_denied"}),
        response(200, {"access_token": "access-secret", "expires_in": 3600}),
    ):
        http_client = RecordingFormHttpClient(token_response)
        client = XaiOAuthDeviceClient(
            client_id="public-client-id",
            http_client=http_client,
            now=lambda: now,
            sleep=lambda seconds: None,
        )
        api = FakeCredentialApi()
        store = WindowsOAuthCredentialStore(profile_id="default", api=api)

        with pytest.raises(CredentialUnavailable):
            client.complete_device_authorization(
                authorization,
                store=store,
                lock=RecordingLock(),
            )

        assert api.writes == []
