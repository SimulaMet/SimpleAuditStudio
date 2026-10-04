# Chat (Open WebUI)

A module that embeds [Open WebUI](https://openwebui.com) in Studio, signed in
as the Studio user — on **one origin, one port**. Studio's wrapper page
(model-picker bar + iframe) lives at `/ai/`; Open WebUI itself serves its whole
app at `/chat/` on the same origin, via the subpath fork
([`deploy/openwebui-subpath/`](../deploy/openwebui-subpath/README.md), built
with `WEBUI_SUBPATH=/chat`). It lives in one Django app, `chat/`:

```
chat/
  config.py          what the module is configured to do, and who you are
  views.py urls.py   the /ai/ wrapper page, /chat/authz, /chat/css-mask
  proxy.py           embedded front door (bundled Caddy, Python fallback)
                     + Open WebUI's lifecycle
  api.py             talking to Open WebUI's API, both directions
  management/        sync_chat_models, chat_knowledge
  templates/ tests/
```

It is part of the bundle the local one-liner starts
and of the Compose deployment `.env.example` describes, and it can be left out
entirely: with `SIMPLEAUDIT_CHAT` set to `off` (or `disabled`, `false`, `no`,
`0`) or unset in a deployment that does not set it, `/ai/` and `/chat/authz`
return 404, the sidebar has no Chat entry, and nothing extra runs.

## Why one origin, and how the subpath works

Upstream Open WebUI serves from the root of an origin only — proxying it under
`https://studio/chat/` serves a broken page. So Studio runs a small fork
rebased on pinned upstream tags, built with a base path:

- the fork's SvelteKit build is configured with `paths.base` and its
  `goto()`/`href`/static-asset references all respect `WEBUI_SUBPATH=/chat`,
  so every browser-visible URL carries the `/chat/` prefix (HTML, `/_app`,
  `/static`, `/api`, the Socket.IO websocket at `/chat/ws/socket.io`);
- Caddy (the single published port) routes `/chat/*` **without stripping the
  prefix** — the build expects to be served at `/chat`; stripping breaks the
  websocket (verified empirically);
- the browser sees one origin, the session cookie rides along in the same-origin
  iframe, and Caddy's `forward_auth` against `/chat/authz` injects the trusted
  identity headers.

The only remaining cross-process hop is that Caddy injects the identity headers
— Open WebUI's trusted-header mode requires a proxy in front, and that proxy
must be the only route to Open WebUI (see Security below). Both modes use the
same Caddy forward-auth: docker mode ships it as a compose service; embedded
mode runs the bundled `caddyserver` wheel (all platforms) and fails loudly at
startup if its binary is missing (a broken install).

## Hiding the sidebar in the iframe

Open WebUI has no embed mode, but its app shell loads `/static/custom.css` on
every page. The front door intercepts that one request and hands it to
Studio's `/chat/css-mask` instead of forwarding it, so the iframe renders
without the chat-history sidebar (both its collapsed rail and expanded panel
are hidden; the chat fills the width). The rule lives in
this repo, not in a copy of Open WebUI, so it survives upgrades in both modes
— in docker mode Caddy proxies the request to Studio; in embedded mode the
bundled Caddy (or the Python fallback handler) does the same.

The endpoint is session-aware: Studio's admin pages (Knowledge, Tools) load
their iframe with `?embed=admin`, which `/chat/authz` stamps into the session;
the `/static/custom.css` load that follows then gets
[chat/embed_admin.css](../chat/embed_admin.css) — which shows only the target
workspace section, with no Open WebUI tab bar — while everyone else (including
the plain `/ai/` chat) gets
[chat/embed.css](../chat/embed.css). The flag expires after ~30s so a
workspace visit doesn't re-skin a later plain chat view. To change what the
iframe shows, edit those files.

## How sign-on works

Open WebUI's *trusted header* mode: it accepts the identity of whoever calls it
in HTTP headers. A proxy in front of it decides that identity by asking Studio,
using the standard forward-auth contract.

```
browser ──► front door (Caddy :8000 in both modes — the only published port)
              │  strips any client-supplied X-Studio-* header
              │
              ├──► Studio  GET /chat/authz   (browser cookies forwarded)
              │       401 -> 302 the browser to /login/
              │       200 -> X-Studio-Email, X-Studio-Name, X-Studio-Role
              │
              ├──► /chat/*  → Open WebUI (internal :8080, at /chat; no strip)
              │
              └──► everything else → Studio app (internal :8001)
```

Both modes share this exact topology: **Caddy** is the single published port,
Open WebUI sits at `/chat` on an internal `:8080`, and Studio the app sits
behind the front door. In docker mode each is its own container (Studio on
`web:8000`, the Caddy service publishing `:8000`); in embedded mode they share
the host, so Studio moves to `:8001` and Caddy takes `:8000`. The front door
is the same Caddyfile in both — docker mode ships
`deploy/openwebui-subpath/Caddyfile.subpath` as a compose service, embedded
mode generates the equivalent in `chat/proxy.py` from the bundled `caddyserver`
wheel (all platforms).

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
loopback; the compose profile publishes no port for it. Every front door — the
docker Caddy, the embedded bundled Caddy, and the embedded Python fallback —
strips client-supplied `X-Studio-*` headers before adding its own; if you put
your own proxy in front, it must do the same.

## Local (no Docker)

```bash
uvx simpleaudit-studio                 # chat is part of the bundle
uvx simpleaudit-studio --disable-chat  # leave it out
```

Starts Open WebUI on 127.0.0.1:8080 (at `/chat`), Studio the app on 127.0.0.1:8001,
and the front door — the bundled Caddy binary (the `caddyserver` wheel, all
platforms) with the same forward-auth Caddyfile the docker deployment runs — on
**:8000**, the single published port. That Caddyfile is generated by
`chat/proxy.py` and logged to `openwebui/caddy.log`; if the binary cannot be
located, `chat/proxy.py` falls back to its built-in Python handler instead, which
serves the same routes. Because everything sits behind one origin, the `/ai/`
page's iframe points at the same-origin `/chat`.

Open WebUI is launched the way the fork wheel expects: `uvicorn open_webui.main:app`
with `FROM_INIT_PY=true` and `WEBUI_SUBPATH=/chat` (the `open-webui serve` entry
point double-prefixes the base path, so it is not used). Interpreter resolution is
pluggable — a venv with the fork wheel wins, then the current one, then a
managed one — so you can point the front door at a prebuilt wheel without
re-downloading. Open WebUI's data lives beside Studio's, in
`~/.simpleaudit-studio/openwebui/`, and it runs from that folder so its signing
key stays there too.

The CLI reports what it is doing: it says when chat is starting, warns on a first
run that Open WebUI is being downloaded (a few minutes), prints where its data and
log live, and prints one line when the chat is actually ready — or why it stopped.
Open WebUI's own output goes to `openwebui/server.log`, not the console. Studio
and the worker come up while all this happens.

The frontend binds to loopback by default (`--host 127.0.0.1`); only the Caddy
front door on `:8000` is meant to be reachable. If you override the command with
`SIMPLEAUDIT_CHAT_CMD`, pass the host/port yourself — binding Open WebUI to all
interfaces is what the Security section is warning about.

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

The proxy keeps no state between requests — an HTTP client with a cookie jar
would hand one browser's session to the next — except a few seconds of "who is
this cookie" (`SIMPLEAUDIT_CHAT_IDENTITY_TTL`, default 5s, 0 to disable). A page
load pulls dozens of assets, and without it each one would ask Django again; a
sign-out takes effect within that window.

Ollama is switched off (nothing in a Studio deployment serves it). Left on, Open
WebUI polls it on every page load — a failing request in the browser console each
time — and shows an empty Ollama section in its connection settings. The
environment variable only seeds the first start, so a sync also turns it off
through the API.

## Docker

Chat is **on by default** in Compose. A fresh `.env` (from `.env.example`)
already ships with both switches enabled:

```bash
# .env
SIMPLEAUDIT_CHAT=docker                        # web serves /ai/ + /chat/authz
COMPOSE_PROFILES=chat                          # the open-webui container starts
SIMPLEAUDIT_STUDIO_URL=http://localhost:8000   # where signed-out users are sent

docker compose up -d
```

To run Compose without chat, set `SIMPLEAUDIT_CHAT=off` and comment out
`COMPOSE_PROFILES=chat` (or remove `chat` from it), then `docker compose up -d`
again. (`.env` files that predate this default need the two lines added.)

The two switches are independent, and setting only `SIMPLEAUDIT_CHAT` gives an
`/ai/` page with nothing behind it.

`chat-proxy` — a Caddy container configured by
[deploy/openwebui-subpath/Caddyfile.subpath](../deploy/openwebui-subpath/Caddyfile.subpath)
— is the **only published port** (`:8000`), in every configuration: it fronts
Studio, Studio's `/ai/` wrapper, and (with the chat profile) the subpath
`open-webui` container (no published port). `SIMPLEAUDIT_CHAT_URL` is left
unset: the iframe loads the same-origin `/chat/`.

## Settings

| Variable                        | Default                  | Meaning                                    |
|---------------------------------|--------------------------|--------------------------------------------|
| `SIMPLEAUDIT_CHAT`              | `embedded` (CLI), unset elsewhere | `embedded`, `docker`, or `off`/`disabled`/`false`/`no`/`0` |
| `SIMPLEAUDIT_CHAT_URL`          | unset → same-origin `/chat` | full URL only for a legacy own-origin deployment |
| `SIMPLEAUDIT_CHAT_UPSTREAM`     | `http://127.0.0.1:8080/chat` | where Open WebUI listens (docker: `http://open-webui:8080/chat`) |
| `SIMPLEAUDIT_CHAT_PUBLIC_PORT`  | `8000`                   | the front door's (Caddy) port — the only published one, both modes |
| `SIMPLEAUDIT_CHAT_INTERNAL_PORT`| `8001`                   | where Studio the app listens behind the front door |
| `SIMPLEAUDIT_CHAT_UPSTREAM_PORT`| `8080`                   | Open WebUI's port (docker mode)            |
| `SIMPLEAUDIT_STUDIO_URL`        | `http://localhost:8000`  | where signed-out users are sent (docker)   |
| `SIMPLEAUDIT_CHAT_CMD`          | auto                     | command that starts Open WebUI             |
| `SIMPLEAUDIT_CHAT_IDENTITY_TTL` | `5`                      | seconds the proxy caches who a cookie is   |
| `SIMPLEAUDIT_CHAT_SYNC_DELAY`   | `2`                      | seconds a model-connection push waits      |
| `SIMPLEAUDIT_CHAT_OTLP`         | `false`                  | export Open WebUI's spans to Studio's OTLP listener |
| `SIMPLEAUDIT_CHAT_OTLP_ENDPOINT`| full OTLP listener URL   | the `/otlp/v1/traces` ingestion URL (embedded: `http://127.0.0.1:<port>/otlp/v1/traces`, docker: `http://web:8000/otlp/v1/traces`) — the exporter uses it as-is |
| `SIMPLEAUDIT_CHAT_OTLP_SERVICE_NAME` | `open-webui`         | the service name Open WebUI tags its spans with |

## Exporting Open WebUI's spans to Studio (OTLP)

Open WebUI can emit OpenTelemetry traces. When `SIMPLEAUDIT_CHAT_OTLP` is set
to `true`, Studio starts it with tracing enabled and pointed at Studio's own
OTLP listener (`POST /otlp/v1/traces`), so the spans it emits land in the same
place as any other target's — no separate collector needed.

It is off by default. When enabled it exports **unauthenticated** by default —
no credentials are sent — which matches the listener's default fallback to an
enabled `none` credential. The exporter is the standard OTel one, so the
environment variables are the standard ones, with two Open WebUI specifics:

- Open WebUI selects the HTTP exporter from `OTEL_OTLP_SPAN_EXPORTER`
  (`http`), **not** the standard `OTEL_EXPORTER_OTLP_PROTOCOL`. `http`
  selects the **protobuf** wire format (`application/x-protobuf`), so
  `OTEL_EXPORTER_OTLP_PROTOCOL` is ignored — the listener accepts both
  JSON and protobuf.
- Open WebUI passes `OTEL_EXPORTER_OTLP_ENDPOINT` explicitly to its OTLP
  exporter (see `utils/telemetry/setup.py` in the Open WebUI backend), and an
  explicit endpoint is used **as-is** — nothing is appended. So the endpoint
  must be the **full** ingestion URL, e.g.
  `https://<studio>/otlp/v1/traces` (a base-only URL 404s).

So enabling it is one line:

```bash
# .env (docker) — or the equivalent environment in embedded mode
SIMPLEAUDIT_CHAT_OTLP=true
```

For an authenticated target (a `basic` or `bearer` OTLP credential instead of a
`none` one), set the matching variables — embedded mode reads them from the
environment it starts Open WebUI with, docker mode passes them straight through:

```bash
OTEL_BASIC_AUTH_USERNAME=sa_<target_id>     # from the credential
OTEL_BASIC_AUTH_PASSWORD=<password>          # shown once at creation
```

If no enabled `none` credential exists and no auth is set, the listener answers
401 and Open WebUI drops the spans.

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

### Agents, knowledge bases and tools

Studio's `/agents/` resources stay thin references: the durable config lives
in Studio, and the executable copy lives in Open WebUI. Every create/update
pushes to Open WebUI (best-effort, after the Studio row is saved), and every
delete propagates:

- **Agents** become *workspace model* entries (`studio.agent-<pk>`) that
  inherit the base connection model and attach the agent's knowledge bases
  via the model's `meta.knowledge`. Chat "Test in Chat" pins that entry.
- **Knowledge bases** and **tools** are created/renamed in Open WebUI's own
  knowledge base and toolkit stores; the local row keeps the returned id in
  `external_id` for listing. Tool *source code* is never stored in Studio —
  Open WebUI owns it, and a metadata-only Studio edit re-pushes the untouched
  remote source.

The sync is best-effort: a down Open WebUI is logged, not fatal, and the next
edit retries. The agent
detail page and `GET /api/agents/<id>/` (`openwebui_live`) show the live
Open WebUI entry, falling back to the cached Studio row when it is
unreachable. When chat is disabled everything is a no-op and Studio works
standalone.

## Removing it

Set `SIMPLEAUDIT_CHAT=disabled`, or pass `--disable-chat` to the CLI.

To drop the code, delete the `chat/` app and `deploy/openwebui-subpath/`,
then remove its four references: `"chat"` in `INSTALLED_APPS`, the `chat/` route
in `config/urls.py`, the Chat entry in `infra/context_processors.py`, the
`--chat` flag in `simpleaudit_studio/cli.py`, and the `chat` profile in
`docker-compose.yml`. Nothing else refers to it.
