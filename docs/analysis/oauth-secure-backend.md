# OAuth secure backend research

## Scope

Исследование выполнено 17 июля 2026 для concrete Windows OAuth vertical LenkoBot. Оно уточняет только secure storage, межпроцессную блокировку и device-code protocol; upstream-код не переносился.

## Sources

- Microsoft Credential Manager: `CredReadW`, `CredWriteW`, `CredFree`, `CRED_TYPE_GENERIC`, `CRED_MAX_CREDENTIAL_BLOB_SIZE` — https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreadw
- Microsoft Win32 mutex wait: `WaitForSingleObject`, `WAIT_ABANDONED`, `WAIT_TIMEOUT`, `WAIT_FAILED` — https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject
- RFC 8628 OAuth 2.0 Device Authorization Grant — https://www.rfc-editor.org/rfc/rfc8628.html
- Live xAI OIDC discovery: `https://auth.x.ai/.well-known/openid-configuration`
- Existing local audit: [xai-oauth.md](xai-oauth.md)

## Findings

- Windows Credential Manager can store an application-defined generic credential blob. LenkoBot stores one UTF-8 JSON `OAuthTokenState`, with a versioned target name and a 2560-byte maximum blob guard. `CredReadW` results must be released with `CredFree`; missing credentials map to an empty store result.
- Refresh serialization uses a named `Local\\` mutex. `WAIT_ABANDONED` grants ownership and the caller must re-read state; timeout/failure is a controlled unavailable error. The existing coordinator keeps the complete read-refresh-persist cycle under this lock.
- xAI device authorization uses `https://auth.x.ai/oauth2/device/code` and token polling uses `https://auth.x.ai/oauth2/token`. The client ID and scopes remain configuration, not LenkoBot-owned constants. The adapter accepts only the approved HTTPS hosts on the default port.
- Device polling handles RFC 8628 `authorization_pending` and cumulative `slow_down`; terminal errors do not write token state. Malformed response durations and untrusted verification URIs fail as controlled credential errors. Exact xAI entitlement/error ownership and public client ID stability remain Open.

## Applicability

This note supports the Windows adapter in the current local-first deployment. Docker/VPS secret backends, token revocation, account switching and device-login presentation UX require separate decisions. The implementation uses injected fakeable API protocols in tests and does not create credentials during test runs.
