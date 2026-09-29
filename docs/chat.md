# Chat (Open WebUI)

A module that embeds [Open WebUI](https://openwebui.com) in Studio at `/chat/`,
signed in as the Studio user. It is part of the bundle the local one-liner starts
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
and it runs from that folder so its signing key stays there too. The first start
downloads it (a few hundred MB), so `/chat/` stays blank for a minute or two.

`open-webui serve` ignores `HOST`/`PORT` and defaults to **0.0.0.0**:8080, so
Studio passes `--host`/`--port` explicitly. If you override the command with
`SIMPLEAUDIT_CHAT_CMD`, pass those flags yourself — binding it to all interfaces
is what the warning above is about.

WebSockets are not proxied; Open WebUI's Socket.IO client falls back to HTTP
long-polling. Chat responses stream over SSE and are unaffected.

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

## Removing it

Set `SIMPLEAUDIT_CHAT=disabled`, or pass `--disable-chat` to the CLI. To drop the code, delete `infra/chat.py`,
`infra/chat_proxy.py`, `infra/tests/test_chat.py`, `templates/chat.html`,
`deploy/compose/Caddyfile.chat`, the two `chat/` routes in `config/urls.py`, the
Chat entry in `infra/context_processors.py`, the `--chat` flag in
`simpleaudit_studio/cli.py` and the `chat` profile in `docker-compose.yml`.
Nothing else refers to it.
