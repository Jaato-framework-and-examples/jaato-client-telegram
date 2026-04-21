# WS Client Authentication

This document describes how jaato-server's WebSocket endpoint handles
authentication, how the `telegram-bot` client authenticates, and how to
run auth-less when appropriate.

## How WS Auth Works

The jaato-server WS endpoint (`--web-socket :PORT`) does **not** use
HTTP-level auth (no `Authorization` header on the upgrade request).  The
WebSocket handshake is unauthenticated.  After the TCP connection is
established and the HTTP 101 Switching Protocols response is received, the
client sends its first message as a JSON frame:

```json
{"type": "auth.token", "token": "<Keycloak access_token JWT>"}
```

The server (via `jaato-premium/session_reconnect/extension.py`) validates
the JWT:

1. Reads the `auth` section from `~/.jaato/servers.json` to get the
   Keycloak issuer URL.
2. Fetches `/.well-known/openid-configuration` and the JWKS keys.
3. Decodes and validates the JWT with `authlib.jose.jwt.decode(token, jwks)`.
4. Calls `ws_server.set_client_user(client_id, preferred_username)`.

On success the server replies:

```json
{"type": "auth.token", "user_id": "service-account-telegram-bot"}
```

On failure:

```json
{"type": "auth.token", "error": "invalid or expired token"}
```

## What Auth Actually Protects

Auth provides **inter-user session isolation** within the same server:
an authenticated user A cannot attach, snapshot, or delete user B's
sessions.  The check (extension.py line 247) is:

```python
if user_id and journal.created_by and journal.created_by != user_id:
    # reject
```

If either side is `None`, the check short-circuits and access is allowed.

Auth does **not**:
- Prevent anonymous connections from creating sessions.
- Prevent anonymous connections from reading other anonymous sessions.
- Gate individual commands by role or scope.
- Gate `message.send` or `tool.execute_request`.

## Running Auth-Less

For local/VPN deployments behind a firewall, auth can be omitted entirely.
The server treats unauthenticated connections as anonymous (no user
restriction).

### Option 1: Don't send auth.token (simplest)

The client simply connects and starts sending commands without the
`auth.token` frame.  The server assigns `user_id = None` and all ownership
checks short-circuit.

### Option 2: Remove the auth section from servers.json

Delete the `auth` key from `~/.jaato/servers.json`.  The server's
`_get_auth_config()` returns `None`, so `auth.token` frames are rejected
with "invalid or expired token" — but clients that skip auth entirely
proceed normally.

### Option 3: Don't load the session_reconnect extension

Remove `session_reconnect` from `[project.entry-points."jaato.extensions"]`
in `jaato-premium/pyproject.toml` (or don't install `jaato-premium` at all).
No `auth.token` handler exists; the message gets a "no handler" response.

### Option 4: Permissive Keycloak

Point the issuer at a Keycloak realm with `publicClient: true`.  Tokens
are issued freely; validation still succeeds.  "Still using Keycloak" but
with zero practical gate.

## WSTransport Configuration

`WSTransport` supports both modes:

### Auth-less (default)

```python
transport = WSTransport(url="ws://localhost:8089")
await transport.connect()  # No auth.token sent
```

### With Keycloak auth

```python
transport = WSTransport(
    url="ws://localhost:8089",
    keycloak_base_url="https://localhost:8180",
    keycloak_realm="jaato",
    keycloak_client_id="telegram-bot",
    keycloak_client_secret="<client-secret>",
)
await transport.connect()  # Fetches JWT, sends auth.token, waits for reply
print(transport.user_id)   # "service-account-telegram-bot"
```

## Keycloak Service Account Setup

The `telegram-bot` client is configured in Keycloak as:

- **Realm:** `jaato`
- **Client ID:** `telegram-bot`
- **Authenticator type:** `client-secret` (not `client-secret-post`)
- **Public client:** false
- **Service accounts enabled:** true

### Creating the client (admin API)

```bash
# Get admin token
ADMIN_TOKEN=$(curl -sk -X POST   https://localhost:8180/realms/master/protocol/openid-connect/token   -d 'grant_type=password&client_id=admin-cli&username=admin&password=admin'   | jq -r .access_token)

# Create client
curl -sk -X POST   https://localhost:8180/admin/realms/jaato/clients   -H "Authorization: Bearer $ADMIN_TOKEN"   -H "Content-Type: application/json"   -d '{
    "clientId": "telegram-bot",
    "publicClient": false,
    "serviceAccountsEnabled": true,
    "clientAuthenticatorType": "client-secret",
    "directAccessGrantsEnabled": true
  }'

# Get the auto-generated secret
CLIENT_UUID=$(curl -sk -H "Authorization: Bearer $ADMIN_TOKEN"   "https://localhost:8180/admin/realms/jaato/clients?clientId=telegram-bot"   | jq -r '.[0].id')

curl -sk   "https://localhost:8180/admin/realms/jaato/clients/$CLIENT_UUID/client-secret"   -H "Authorization: Bearer $ADMIN_TOKEN"
```

### Obtaining a token

```bash
curl -sk -X POST   https://localhost:8180/realms/jaato/protocol/openid-connect/token   -d "grant_type=client_credentials"   -d "client_id=telegram-bot"   -d "client_secret=<secret>"
```

### Stored in pass

Credentials are stored in the Unix `pass` password manager:

```
jaato/keycloak/server-url        → https://localhost:8180
jaato/keycloak/realm              → jaato
jaato/keycloak/telegram-bot/client-id    → telegram-bot
jaato/keycloak/telegram-bot/client-secret → <secret>
jaato/keycloak/telegram-bot/token        → <JWT (ephemeral)>
```

### Common Keycloak pitfalls

- **`clientAuthenticatorType` must be `client-secret`**, not
  `client-secret-post`.  The latter works for ROPC but fails for
  `client_credentials` grant with "Invalid client" in the server logs.
- **Secret is regenerated** when changing authenticator type.  Always
  re-fetch after any client config change via `GET .../client-secret`.
- **Realm path** for Keycloak 17+ is `/realms/jaato/...` (no `/auth`
  prefix).

## Security Framing

For a local daemon behind a firewall or on a trusted VPN, running
auth-less is fine and matches how many deployments work.  If the WS
port is exposed to untrusted networks, auth-less is actively unsafe
because anyone who can open a TCP connection can drive the agent — and
the agent has shell access, filesystem, and credentials.  The AppArmor
profile is the primary security boundary, not WS auth.
