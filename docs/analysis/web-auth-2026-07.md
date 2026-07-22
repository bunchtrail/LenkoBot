# Phase 6 web authentication и server-rendered stack

## Источник и границы

Проверка выполнена 21 июля 2026:

- [Telegram Login/OIDC](https://core.telegram.org/widgets/login)
- [Telegram legacy Login Widget](https://core.telegram.org/widgets/login-legacy)
- [Telegram OIDC discovery](https://oauth.telegram.org/.well-known/openid-configuration)
- [Starlette 1.3.1](https://github.com/Kludex/starlette/releases/tag/1.3.1)
- [Uvicorn 0.51.0](https://github.com/Kludex/uvicorn/releases/tag/0.51.0)
- [Authlib 1.7.2](https://github.com/authlib/authlib/releases/tag/v1.7.2)
- [OWASP CSRF](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [OWASP HTTP headers](https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html)
- [Cloudflare Tunnel ingress](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/)
- [Cloudflare Tunnel DNS](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/routing-to-tunnel/dns/)
- [Cloudflare HTTP headers](https://developers.cloudflare.com/fundamentals/reference/http-headers/)

Исследование покрывает single-owner server-rendered panel. Реальный BotFather
OIDC registration и stable tunnel hostname остаются external gates.

## Решение

`Confirmed`: Telegram OIDC Authorization Code + PKCE, server-rendered
`Starlette==1.3.1`, `Jinja2==3.1.6`, `uvicorn==0.51.0`, `Authlib==1.7.2` и
`httpx==0.28.1`. Legacy widget не является primary path.

- `GET /login` создаёт durable single-use attempt: state hash, browser nonce
  hash, PKCE verifier, OIDC nonce hash, local return route и 10-minute expiry.
- Callback atomically consume-ит attempt до code exchange, проверяет ID-token
  signature через JWKS, exact issuer/audience, expiry и nonce, затем сравнивает
  `sub` с configured Telegram owner ID.
- Invalid, expired, replayed и wrong-owner callbacks возвращают одинаковый
  generic outcome и не создают session.
- Redirect destination хранится как allowlisted local route; request не может
  передать external URL, `//`, backslash или authority.

## Session и CSRF contract

- Session token и CSRF token — independent 32-byte random values; SQLite хранит
  только hashes, owner ID, idle/absolute expiry и revocation.
- Cookie: `__Host-lenkobot_session`, `Secure`, `HttpOnly`, `SameSite=Lax`,
  `Path=/`, без `Domain`.
- Mutations используют server-side synchronizer CSRF token в hidden form field,
  constant-time comparison, Origin/Fetch-Metadata defense-in-depth и только POST.
- Full reset/revocation немедленно делает все server-side sessions unusable.

## Web security contract

- External origin задаётся явно и не выводится из `Host`, `X-Forwarded-*` или
  `CF-*` headers.
- Sensitive responses: `Cache-Control: no-store`, CSP `default-src 'self'`,
  `base-uri/object-src/frame-ancestors 'none'`, `form-action 'self'`, DENY frame,
  `nosniff`, `no-referrer`, disabled camera/geolocation/microphone, noindex.
- App bind — loopback/Unix socket; Cloudflare ingress сопоставляет только exact
  panel hostname и заканчивается catch-all `http_status:404`.
- Cloudflare source IP headers не являются authentication input.

## Remaining external gates

- BotFather должен выдать OIDC client ID/secret и зарегистрировать exact origin
  и callback URL.
- Stable Cloudflare hostname нужен для production-like login/tunnel smoke.
- До этих данных unit/integration используют deterministic fake OIDC port; runtime
  fail-closed не публикует panel без complete config.
