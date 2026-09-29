# Chat (Open WebUI)

A module that embeds [Open WebUI](https://openwebui.com) in Studio at `/chat/`,
signed in as the Studio user. It lives in one Django app, `chat/`:

```
chat/
  config.py          what the module is configured to do, and who you are
  views.py urls.py   the iframe page and /chat/authz
  proxy.py           the forward-auth proxy + Open WebUI's lifecycle (embedded)
  api.py             talking to Open WebUI's API, both directions
  management/        sync_chat_models, chat_knowledge
  templates/ tests/
```

It is part of the bundle the local one-liner starts
and of the Compose deployment `.env.example` describes, and it can be left out
entirely: with `SIMPLEAUDIT_CHAT` set to `off` (or `disabled`, `false`, `no`,
`0`) or unset in a deployment that does not set it, `/chat/` and `/chat/authz`
return 404, the sidebar has no Chat entry, and nothing extra runs.

## Why it is an iframe, not a sub-path

Open WebUI serves from the root of an origin only. It has no base-path setting,
and its HTML references `/static`, `/api` and `/ws` absolutely, so proxying it
under `https://studio/chat/` serves a broken page. It therefore gets its own
origin (a port locally, a host in production) which Studio embeds.

## How sign-on works

Open WebUI's *trusted header* mode: it accepts the identity of whoever calls it
in HTTP headers. A proxy in front of it decides that identity by asking Studio,
using the standard forward-auth contract.

```
browser ──► proxy (:8801)
              │  strips any client-supplied X-Studio-* header
              │
              ├──► Studio  GET /chat/authz   (browser cookies forwarded)
              │       401 -> proxy redirects the browser to Studio
              │       200 -> X-Studio-Email, X-Studio-Name, X-Studio-Role
              │
              └──► Open WebUI (127.0.0.1:8080, no published port)
```

Django remains the only authority on identity; nothing outside it reads sessions
or user tables. Open WebUI creates its account on the first request per user and
keeps its own database of chats and settings.

Role mapping (`X-Studio-Role`, applied on every sign-in):

| Studio                                   | Open WebUI |
|------------------------------------------|------------|
| superuser, or admin of any workspace      | `admin`    |
| everyone else                             | `user`     |

## Security

**Open WebUI must be reachable only from the proxy.** It believes the headers on
any request it receives, so a client that can connect to it directly can send
`X-Studio-Role: admin` and take over the instance. Embedded mode binds it to
loopback; the compose profile publishes no port for it. Both the Python proxy and
the Caddy config strip client-supplied `X-Studio-*` headers before adding their
own — if you put your own proxy in front, it must do the same.

## Local (no Docker)

```bash
uvx simpleaudit-studio                 # chat is part of the bundle
uvx simpleaudit-studio --disable-chat  # leave it out
```

Starts Open WebUI (via `open-webui` if installed, otherwise `uvx`) on
127.0.0.1:8080, plus the forward-auth proxy from `infra/chat_proxy.py` on :8801.
Open WebUI's data lives beside Studio's, in `~/.simpleaudit-studio/openwebui/`,
and it runs from that folder so its signing key stays there too.

The CLI reports what it is doing: it says when chat is starting, warns on a first
run that Open WebUI is being downloaded (~1 GB via `uvx`, a few minutes), prints
where its data and log live, and prints one line when `/chat/` is actually ready
— or why it stopped. Open WebUI's own output goes to `openwebui/server.log`, not
the console. Studio and the worker come up while all this happens.

`open-webui serve` ignores `HOST`/`PORT` and defaults to **0.0.0.0**:8080, so
Studio passes `--host`/`--port` explicitly. If you override the command with
`SIMPLEAUDIT_CHAT_CMD`, pass those flags yourself — binding it to all interfaces
is what the warning above is about.

Open WebUI is managed like the embedded Hatchet engine: one instance per Studio
process, started in its own process group, and stopped on the way out — by the
CLI's shutdown and by `atexit`, so Ctrl+C, `kill`, and an unhandled exit all take
it with them. The group matters because `uvx` is only a launcher; signalling it
alone would leave the server running.

A run that is hard-killed (SIGKILL, a crash, a closed terminal) cannot stop
anything, so its Open WebUI keeps holding the port. The next start finds it
through `openwebui/open-webui.pid` and stops it first — but only when it is a
genuine leftover, i.e. its parent is gone. One that belongs to another running
Studio is left alone, and that start fails on the port instead.

WebSocket upgrades are tunnelled: the handshake is forwarded with the identity
headers attached, and once Open WebUI answers 101 the two sockets are piped
together — nothing in the proxy understands WebSocket framing. Socket.IO
therefore behaves as it does behind Caddy instead of falling back to polling.

Ollama is switched off (nothing in a Studio deployment serves it). Left on, Open
WebUI polls it on every page load — a failing request in the browser console each
time — and shows an empty Ollama section in its connection settings. The
environment variable only seeds the first start, so a sync also turns it off
through the API.

## Docker

`.env.example` ships with both switches set, so the ordinary command starts chat
too:

```bash
# .env
SIMPLEAUDIT_CHAT=docker                        # web serves /chat/
COMPOSE_PROFILES=chat                          # the two chat containers start
SIMPLEAUDIT_CHAT_URL=http://localhost:8801     # what the browser opens
SIMPLEAUDIT_STUDIO_URL=http://localhost:8000   # where signed-out users are sent

docker compose up -d
```

Comment both switches out to deploy without chat; they are independent, and
setting only `SIMPLEAUDIT_CHAT` gives a `/chat/` page with nothing behind it.

This runs `open-webui` (no published port) behind `chat-proxy`, a Caddy container
configured by [deploy/compose/Caddyfile.chat](../deploy/compose/Caddyfile.chat).
Studio itself only serves the iframe page and `/chat/authz`.

On separate hostnames (`studio.example.com` / `chat.example.com`), set
`SESSION_COOKIE_DOMAIN=.example.com` so the proxy receives Studio's session
cookie. Different registrable domains will not work.

## Settings

| Variable                        | Default                  | Meaning                                    |
|---------------------------------|--------------------------|--------------------------------------------|
| `SIMPLEAUDIT_CHAT`              | `embedded` (CLI), unset elsewhere | `embedded`, `docker`, or `off`/`disabled`/`false`/`no`/`0` |
| `SIMPLEAUDIT_CHAT_URL`          | `http://localhost:8801`  | the origin the iframe loads                |
| `SIMPLEAUDIT_CHAT_UPSTREAM`     | `http://127.0.0.1:8080`  | where Open WebUI listens                   |
| `SIMPLEAUDIT_CHAT_PROXY_PORT`   | `8801`                   | the proxy's port (both modes)              |
| `SIMPLEAUDIT_CHAT_UPSTREAM_PORT`| `8080`                   | Open WebUI's port (docker mode)            |
| `SIMPLEAUDIT_STUDIO_URL`        | `http://localhost:8000`  | where signed-out users are sent (docker)   |
| `SIMPLEAUDIT_CHAT_CMD`          | auto                     | command that starts Open WebUI             |

## Syncing with Studio (scaffolding)

`chat/api.py` talks to Open WebUI's API in both directions. It authenticates the
same way the proxy makes the browser authenticate — POST the trusted identity
headers to `/api/v1/auths/signin`, use the token that comes back — so there is no
API key to provision and every call runs as a real Open WebUI user with that
user's role.

**Push — Studio model connections become Open WebUI providers.** A Studio
connection is a base URL plus a key, which is exactly Open WebUI's
OpenAI-compatible provider config (`OPENAI_API_BASE_URLS` / `OPENAI_API_KEYS` /
`OPENAI_API_CONFIGS`):

```bash
python manage.py sync_chat_models --dry-run   # show what would be pushed
python manage.py sync_chat_models             # push every enabled connection
python manage.py sync_chat_models --project demo
```

Those lists are also editable by hand in Open WebUI, so each pushed entry carries
a `simpleaudit_connection_id` marker in its config. A sync replaces the marked
entries and leaves everything else where it is — the command says how many of
each. Pushing provider config needs an Open WebUI admin, so the command acts as a
Studio superuser.

**Pull — what Open WebUI holds.** Knowledge bases come back as plain dicts, so
Studio code never sees Open WebUI's schema:

```bash
python manage.py chat_knowledge            # id, name, file count
python manage.py chat_knowledge --id <id>  # one, with its file names
```

```python
from chat.api import ChatAPI

bases = ChatAPI.as_user(request.user).knowledge_bases()
```

**The push is automatic.** `chat/signals.py` follows `ModelConnection` and
`RegisteredModel`, so adding a connection, changing a key, disabling one or
registering a model all reach chat on their own. The command stays for a manual
run and for `--dry-run`.

The push is kept off the request's path. It happens `on_commit`, so Open WebUI
never sees a row that was rolled back; in a background thread, so saving does not
wait on a second service; debounced by `SIMPLEAUDIT_CHAT_SYNC_DELAY` (2s), so an
edit that writes a connection and its models is one push; and best-effort — a
chat that is down or still starting is logged and forgotten, because Studio's own
data is the source of truth. The CLI also syncs once as soon as chat answers,
which covers connections that changed while it was off.

A connection's registered models become that provider's `model_ids` in Open
WebUI, so chat offers what Studio registered. A connection with no registered
models is left unrestricted.

## Removing it

Set `SIMPLEAUDIT_CHAT=disabled`, or pass `--disable-chat` to the CLI.

To drop the code, delete the `chat/` app and `deploy/compose/Caddyfile.chat`,
then remove its four references: `"chat"` in `INSTALLED_APPS`, the `chat/` route
in `config/urls.py`, the Chat entry in `infra/context_processors.py`, the
`--chat` flag in `simpleaudit_studio/cli.py`, and the `chat` profile in
`docker-compose.yml`. Nothing else refers to it.
