# EclipseAPI

A single Python process that runs both:
- a **Discord bot** that tracks member presence/status in one guild
- a **Flask API server** exposing that status over HTTP

The bot keeps a short-lived in-memory cache (kept warm automatically via
Discord's presence-update events). When the API gets a `/fetch` request,
it checks the cache first — and if the entry is missing or stale, the bot
does a **live lookup** against the guild member list, caches the fresh
result, and returns it.

## Requirements

- Python 3.10+
- A Discord bot application ([discord.com/developers/applications](https://discord.com/developers/applications))
- The bot invited to your target server with the **Server Members Intent**
  and **Presence Intent** enabled (both in the Developer Portal, under
  Bot -> Privileged Gateway Intents)

## Install

```bash
pip install discord.py flask
```

## Configure

Edit `config.json`:

```json
{
  "discord_token": "YOUR_BOT_TOKEN_HERE",
  "guild_id": 123456789012345678,
  "api": {
    "host": "0.0.0.0",
    "port": 5000
  },
  "cache": {
    "ttl_seconds": 30
  },
  "rate_limit": {
    "max_requests": 20,
    "window_seconds": 60
  }
}
```

| Field                       | Description                                                                 |
|-----------------------------|------------------------------------------------------------------------------|
| `discord_token`             | Your bot's token from the Developer Portal. **Never commit this.**          |
| `guild_id`                  | The Discord server (guild) ID the bot should track.                        |
| `api.host` / `api.port`     | Where the Flask server listens.                                            |
| `cache.ttl_seconds`         | How long a cached status is considered "fresh" before a live re-check.     |
| `rate_limit.max_requests`   | Max requests allowed per client IP within the rolling window.              |
| `rate_limit.window_seconds` | Length of the rolling window (in seconds) used for rate limiting.          |

## Run

```bash
python3 bot_server.py
```

This starts the Discord bot in a background thread and the Flask API in
the main thread, in the same process.

## API

### `GET /fetch`

Look up a Discord member's current status by user ID.

**Query params:**
- `id` (required) — the Discord user ID (snowflake) to look up

**Example:**
```
GET /fetch?id=131311313131313131
```

Requests are rate-limited per client IP (see `rate_limit` in `config.json`,
default 20 requests / 60 seconds). Exceeding the limit returns `429` with
a `Retry-After` header and a `retry_after_seconds` field in the body.

**Example response (member found):**
```json
{
  "user_id": "131311313131313131",
  "username": "someuser",
  "display_name": "Some User",
  "status": "online",
  "desktop_status": "online",
  "mobile_status": "offline",
  "web_status": "offline",
  "activities": [
    {
      "type": "custom",
      "text": "brb, deploying",
      "emoji": "🚀"
    }
  ],
  "found": true,
  "source": "cache"
}
```

`"source"` tells you whether the data came from the warm cache (`"cache"`)
or a fresh live lookup triggered by this request (`"live"`).

**Example response (member not found):**
```json
{
  "user_id": "131311313131313131",
  "found": false,
  "error": "member not found in guild"
}
```

**Errors:**
- `400` — missing/invalid `id` param
- `429` — rate limit exceeded for this client IP
- `502` — the live lookup to Discord failed

### `GET /health`

Basic liveness/cache-stats check, no auth required:
```json
{
  "status": "ok",
  "bot_ready": true,
  "cache": { "entries": 12, "ttl_seconds": 30 }
}
```

## How the cache works

1. Every presence update the bot receives over the gateway for members in
   the configured guild updates the cache immediately (near real-time,
   no extra API calls).
2. On a `/fetch` call, if a cache entry exists and is younger than
   `cache.ttl_seconds`, it's returned as-is (fast path, no Discord API hit).
3. If the entry is missing or older than the TTL, the bot performs a live
   `guild.get_member()` (falling back to `guild.fetch_member()` if the
   member isn't in its local member cache), stores the result, and returns it.

This keeps steady-state traffic cheap while guaranteeing you never get
data older than your configured TTL for an actively-polled user.

## Notes / gotchas

- **Presence Intent** must be enabled both in the Developer Portal *and*
  via `intents.presences = True` in code (already done here) — without it,
  `member.status` will always show as `offline`/`unknown`.
- Large guilds (75k+ members) may require Discord to approve your bot for
  the privileged intents if it's verified/public.
- The bot only tracks the single guild set in `guild_id`. For multi-server
  support, you'd extend `/fetch` to accept a `guild_id` param too.
- There is no API key or authentication on `/fetch` — anyone who can reach
  the server can query it. The rate limiter is per-IP and in-memory only
  (it resets on restart and doesn't share state across multiple server
  processes), so if you run this publicly you'll likely want a reverse
  proxy (e.g. nginx) in front of it for TLS and an additional layer of
  request control.
- Keep `discord_token` out of version control — consider loading it from
  an environment variable instead of committing a real value into
  `config.json` if this repo is ever pushed anywhere.
