import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

import discord
from flask import Flask, jsonify, request

CONFIG_PATH = Path(__file__).parent / "config.json"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

DISCORD_TOKEN = CONFIG["discord_token"]
GUILD_ID = CONFIG["guild_id"]
API_HOST = CONFIG["api"]["host"]
API_PORT = CONFIG["api"]["port"]
CACHE_TTL = CONFIG["cache"]["ttl_seconds"]
RATE_LIMIT_MAX = CONFIG["rate_limit"]["max_requests"]
RATE_LIMIT_WINDOW = CONFIG["rate_limit"]["window_seconds"]

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hostlada-status")


class StatusCache:
    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._store: dict[int, dict] = {}

    def get(self, user_id: int) -> dict | None:
        with self._lock:
            entry = self._store.get(user_id)
            if entry is None:
                return None
            if time.time() - entry["_cached_at"] > self._ttl:
                return None
            return entry

    def set(self, user_id: int, payload: dict) -> None:
        payload = dict(payload)
        payload["_cached_at"] = time.time()
        with self._lock:
            self._store[user_id] = payload

    def invalidate(self, user_id: int) -> None:
        with self._lock:
            self._store.pop(user_id, None)

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._store), "ttl_seconds": self._ttl}


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float):
        self._max = max_requests
        self._window = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.time()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self._window:
                q.popleft()
            if len(q) >= self._max:
                retry_after = int(self._window - (now - q[0])) + 1
                return False, retry_after
            q.append(now)
            return True, 0


cache = StatusCache(CACHE_TTL)
limiter = RateLimiter(RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)

intents = discord.Intents.default()
intents.members = True
intents.presences = True

bot = discord.Client(intents=intents)


def _serialize_member(member: discord.Member) -> dict:
    activities = []
    for act in member.activities:
        if isinstance(act, discord.Spotify):
            activities.append(
                {
                    "type": "spotify",
                    "title": act.title,
                    "artist": act.artist,
                    "album": act.album,
                }
            )
        elif isinstance(act, discord.CustomActivity):
            activities.append(
                {
                    "type": "custom",
                    "text": act.name,
                    "emoji": str(act.emoji) if act.emoji else None,
                }
            )
        else:
            activities.append(
                {
                    "type": str(act.type).split(".")[-1],
                    "name": getattr(act, "name", None),
                    "details": getattr(act, "details", None),
                    "state": getattr(act, "state", None),
                }
            )

    return {
        "user_id": str(member.id),
        "username": member.name,
        "display_name": member.display_name,
        "status": str(member.status),
        "desktop_status": str(member.desktop_status),
        "mobile_status": str(member.mobile_status),
        "web_status": str(member.web_status),
        "activities": activities,
        "found": True,
    }


async def lookup_member(user_id: int) -> dict:
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return {"found": False, "error": "bot is not in the configured guild"}

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return {"user_id": str(user_id), "found": False, "error": "member not found in guild"}
        except discord.HTTPException as e:
            return {"user_id": str(user_id), "found": False, "error": f"discord api error: {e}"}

    return _serialize_member(member)


@bot.event
async def on_ready():
    log.info(f"Bot logged in as {bot.user} (id={bot.user.id})")
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        log.warning(
            f"Configured guild_id {GUILD_ID} not found among bot's guilds. "
            "Check config.json and make sure the bot is invited to that server."
        )
    else:
        log.info(f"Tracking guild: {guild.name} ({guild.id}) — {guild.member_count} members")


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    if after.guild.id != GUILD_ID:
        return
    cache.set(after.id, _serialize_member(after))


app = Flask(__name__)

bot_loop = None


def run_coro_threadsafe(coro, timeout: float = 10.0):
    import asyncio

    if bot_loop is None:
        raise RuntimeError("bot event loop not ready yet")
    future = asyncio.run_coroutine_threadsafe(coro, bot_loop)
    return future.result(timeout=timeout)


def client_key(req) -> str:
    forwarded = req.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return req.remote_addr or "unknown"


@app.route("/fetch", methods=["GET"])
def fetch_status():
    key = client_key(request)
    allowed, retry_after = limiter.allow(key)
    if not allowed:
        resp = jsonify({"error": "rate limit exceeded", "retry_after_seconds": retry_after})
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry_after)
        return resp

    user_id_raw = request.args.get("id")
    if not user_id_raw:
        return jsonify({"error": "missing required query param 'id'"}), 400

    try:
        user_id = int(user_id_raw)
    except ValueError:
        return jsonify({"error": "'id' must be a numeric discord user id"}), 400

    cached = cache.get(user_id)
    if cached is not None:
        result = dict(cached)
        result["source"] = "cache"
        result.pop("_cached_at", None)
        return jsonify(result)

    try:
        result = run_coro_threadsafe(lookup_member(user_id))
    except Exception as e:
        log.exception("live lookup failed")
        return jsonify({"error": f"lookup failed: {e}"}), 502

    if result.get("found"):
        cache.set(user_id, result)

    result = dict(result)
    result["source"] = "live"
    return jsonify(result)


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "bot_ready": bot.is_ready() if bot_loop else False,
            "cache": cache.stats(),
        }
    )


def main():
    import asyncio

    global bot_loop

    def start_bot():
        global bot_loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot_loop = loop
        try:
            loop.run_until_complete(bot.start(DISCORD_TOKEN))
        finally:
            loop.close()

    bot_thread = threading.Thread(target=start_bot, daemon=True)
    bot_thread.start()

    log.info("Starting Discord bot thread, waiting for it to come online...")
    for _ in range(100):
        if bot_loop is not None and bot.is_ready():
            break
        time.sleep(0.1)

    log.info(f"Starting API server on {API_HOST}:{API_PORT}")
    app.run(host=API_HOST, port=API_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
