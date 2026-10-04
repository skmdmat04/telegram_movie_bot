"""
Telegram bot: search public-domain movies on the Internet Archive
and reply with a direct download link.

Safety design
-------------
Archive.org's movie collections are *not* uniformly curated — some
(e.g. "feature_films") accept self-tagged uploads, and in practice that
bucket contains a mix of genuine public-domain classics *and* copyrighted
titles uploaded without authorization. To avoid ever surfacing the latter,
every result must satisfy one of:

  1. It's in the `prelinger` collection — Rick Prelinger's own donated
     archive, documented and distributed as public domain regardless of
     per-item license metadata.
  2. It has an explicit Creative Commons / public-domain `licenseurl`.
  3. Its release year is old enough to be in the US public domain by the
     automatic copyright-expiry rule (currently 96 years, recalculated
     at runtime so it doesn't go stale).

Titles that look like piracy scene-release rips (x264, DVDRip, BluRay,
etc.) are excluded outright regardless of the above, and the known
self-upload dump `feature_films_unsorted` is excluded entirely.

This intentionally returns a *smaller* library than "everything tagged
as a movie on archive.org" — that trade-off is the point.
"""
import asyncio
import html
import logging
import os
import random
import re
import secrets
import uuid
from datetime import datetime, timezone

import httpx
from aiohttp import web
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("moviebot")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Set WEBHOOK_URL (e.g. https://your-app.onrender.com) to run in webhook mode
# for a cloud deployment. Leave unset to run in polling mode for local dev.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", "8080"))

# Telegram requires the secret token to match ^[A-Za-z0-9_-]{1,256}$ — some
# hosting platforms' auto-generated env values (e.g. Render's generateValue)
# use a wider charset, so strip anything outside that set rather than fail.
_raw_secret = re.sub(r"[^A-Za-z0-9_-]", "", os.environ.get("WEBHOOK_SECRET") or "")
WEBHOOK_SECRET = _raw_secret or secrets.token_urlsafe(32)

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{filename}"
THUMBNAIL_URL = "https://archive.org/services/img/{identifier}"
ITEM_PAGE_URL = "https://archive.org/details/{identifier}"

# Collection unconditionally trusted as public domain.
TRUSTED_COLLECTION = "prelinger"

# Collections that need a per-item year/license check (see module docstring) —
# these accept open/self-tagged uploads and are not uniformly PD.
CANDIDATE_COLLECTIONS = [
    "feature_films",
    "silent_films",
    "classic_tv",
    "classic_cartoons",
    "animationandcartoons",
    "short_films",
]
# Known self-upload dump inside feature_films; excluded entirely.
EXCLUDED_SUBCOLLECTION = "feature_films_unsorted"

# US works published this many years ago or earlier are automatically
# public domain (rolling window under current copyright law).
PD_AUTO_YEARS = 96

# Piracy scene-release naming patterns — never a legitimate period title.
SCENE_RELEASE_TERMS = [
    "x264", "x265", "hevc", "bluray", "webrip", "camrip", "hdrip",
    "brrip", "hdtc", "dvdrip", "xvid", "hdtv", "webdl", "bdrip",
]

VIDEO_FORMATS = {
    "MPEG4", "h.264", "512Kb MPEG4", "MPEG2", "Matroska", "Ogg Video",
}
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".ogv", ".mpg", ".mpeg", ".m4v")

SEARCH_FETCH_ROWS = 25
SEARCH_PAGE_SIZE = 5
MAX_STORED_SEARCHES_PER_USER = 3

_LUCENE_SPECIAL = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')
_TAG_RE = re.compile(r"<[^>]+>")


def pd_cutoff_year() -> int:
    return datetime.now(timezone.utc).year - PD_AUTO_YEARS


def build_safe_filter() -> str:
    cutoff = pd_cutoff_year()
    candidates = " OR ".join(CANDIDATE_COLLECTIONS)
    bad_terms = " OR ".join(SCENE_RELEASE_TERMS)
    return (
        f"(collection:({TRUSTED_COLLECTION}) OR "
        f"(collection:({candidates}) AND NOT collection:({EXCLUDED_SUBCOLLECTION}) "
        f"AND (year:[1000 TO {cutoff}] OR licenseurl:(*publicdomain* OR *creativecommons*)))) "
        f"AND mediatype:(movies) "
        f"AND NOT title:({bad_terms})"
    )


def escape_lucene_term(term: str) -> str:
    return _LUCENE_SPECIAL.sub(" ", term).strip()


def strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


async def search_movies(client: httpx.AsyncClient, query: str) -> list[dict]:
    words = [escape_lucene_term(w) for w in query.split() if escape_lucene_term(w)]
    if not words:
        return []
    fuzzy_title = " ".join(f"{w}~" for w in words)

    q = f"{build_safe_filter()} AND title:({fuzzy_title})"
    params = {
        "q": q,
        "fl[]": ["identifier", "title", "year"],
        "rows": SEARCH_FETCH_ROWS,
        "page": 1,
        "output": "json",
    }
    resp = await client.get(SEARCH_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("response", {}).get("docs", [])


async def random_movie(client: httpx.AsyncClient) -> dict | None:
    count_params = {"q": build_safe_filter(), "rows": 0, "output": "json"}
    resp = await client.get(SEARCH_URL, params=count_params, timeout=20)
    resp.raise_for_status()
    total = resp.json().get("response", {}).get("numFound", 0)
    if total == 0:
        return None

    start = random.randint(0, min(total, 9999) - 1)
    params = {
        "q": build_safe_filter(),
        "fl[]": ["identifier", "title", "year"],
        "rows": 1,
        "start": start,
        "output": "json",
    }
    resp = await client.get(SEARCH_URL, params=params, timeout=20)
    resp.raise_for_status()
    docs = resp.json().get("response", {}).get("docs", [])
    return docs[0] if docs else None


def item_is_safe(metadata: dict) -> bool:
    """Defense-in-depth re-check at download time, independent of the search filter."""
    collections = metadata.get("collection") or []
    if isinstance(collections, str):
        collections = [collections]

    if TRUSTED_COLLECTION in collections:
        return True
    if EXCLUDED_SUBCOLLECTION in collections:
        return False

    licenseurl = metadata.get("licenseurl") or ""
    if "publicdomain" in licenseurl or "creativecommons" in licenseurl:
        return True

    if extract_year(metadata) is not None and extract_year(metadata) <= pd_cutoff_year():
        return True

    return False


def extract_year(metadata: dict) -> int | None:
    """metadata API sometimes has `year`, sometimes only a `date` string."""
    for key in ("year", "date"):
        value = metadata.get(key)
        if not value:
            continue
        match = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", str(value))
        if match:
            return int(match.group(1))
    return None


async def get_movie_details(client: httpx.AsyncClient, identifier: str) -> dict | None:
    resp = await client.get(METADATA_URL.format(identifier=identifier), timeout=20)
    resp.raise_for_status()
    data = resp.json()
    metadata = data.get("metadata", {})

    title_lower = (metadata.get("title") or "").lower()
    if any(term in title_lower for term in SCENE_RELEASE_TERMS):
        return None
    if not item_is_safe(metadata):
        return None

    best_name = None
    best_size = -1
    for f in data.get("files", []):
        name = f.get("name", "")
        fmt = f.get("format", "")
        if fmt in VIDEO_FORMATS or name.lower().endswith(VIDEO_EXTENSIONS):
            try:
                size = int(f.get("size", 0))
            except (TypeError, ValueError):
                size = 0
            if size > best_size:
                best_name = name
                best_size = size

    if not best_name:
        return None

    return {
        "identifier": identifier,
        "title": metadata.get("title", identifier),
        "year": extract_year(metadata),
        "description": strip_html(metadata.get("description", ""))[:400],
        "download_url": DOWNLOAD_URL.format(identifier=identifier, filename=best_name),
        "item_url": ITEM_PAGE_URL.format(identifier=identifier),
        "thumbnail_url": THUMBNAIL_URL.format(identifier=identifier),
        "size_mb": best_size / (1024 * 1024) if best_size > 0 else None,
    }


def store_search(context: ContextTypes.DEFAULT_TYPE, docs: list[dict]) -> str:
    searches = context.user_data.setdefault("searches", {})
    search_id = uuid.uuid4().hex[:8]
    searches[search_id] = docs
    while len(searches) > MAX_STORED_SEARCHES_PER_USER:
        oldest = next(iter(searches))
        del searches[oldest]
    return search_id


def results_page_markup(search_id: str, docs: list[dict], page: int) -> InlineKeyboardMarkup:
    start = page * SEARCH_PAGE_SIZE
    page_docs = docs[start:start + SEARCH_PAGE_SIZE]

    buttons = []
    for i, doc in enumerate(page_docs, start=start):
        title = doc.get("title", doc["identifier"])
        year = doc.get("year")
        label = f"{title} ({year})" if year else title
        buttons.append([InlineKeyboardButton(label[:64], callback_data=f"get:{search_id}:{i}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Prev", callback_data=f"pg:{search_id}:{page - 1}"))
    total_pages = (len(docs) - 1) // SEARCH_PAGE_SIZE + 1
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if start + SEARCH_PAGE_SIZE < len(docs):
        nav_row.append(InlineKeyboardButton("Next »", callback_data=f"pg:{search_id}:{page + 1}"))
    if len(nav_row) > 1:
        buttons.append(nav_row)

    return InlineKeyboardMarkup(buttons)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎬 Send me a movie title and I'll search public-domain films on the "
        "Internet Archive (archive.org) and send you a direct download link.\n\n"
        "This only covers public-domain / openly-licensed titles — old classics, "
        "silent films, Prelinger educational & industrial films, etc. Not a source "
        "for current copyrighted releases.\n\n"
        "Try /random to discover something, or just type a title."
    )


async def random_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.chat.send_action("typing")
    client: httpx.AsyncClient = context.bot_data["http_client"]

    doc = await random_movie(client)
    if not doc:
        await update.message.reply_text("Couldn't find anything right now, try again.")
        return

    await send_movie_details(update.message.reply_text, update.message.reply_photo, client, doc["identifier"])


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.message.text.strip()
    if not query:
        return

    await update.message.chat.send_action("typing")

    client: httpx.AsyncClient = context.bot_data["http_client"]
    try:
        results = await search_movies(client, query)
    except httpx.HTTPError as e:
        log.warning("search failed: %s", e)
        await update.message.reply_text("Search failed, please try again in a moment.")
        return

    if not results:
        await update.message.reply_text(
            f"No public-domain matches for “{html.escape(query)}”. Try a different "
            "title or spelling — this only searches PD/open-licensed collections, "
            "so many modern titles won't be found. Try /random to browse instead."
        )
        return

    search_id = store_search(context, results)
    await update.message.reply_text(
        f"Found {len(results)} public-domain match(es) — pick one:",
        reply_markup=results_page_markup(search_id, results, page=0),
    )


async def handle_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    _, search_id, page_str = query.data.split(":", 2)
    docs = context.user_data.get("searches", {}).get(search_id)
    if docs is None:
        await query.edit_message_text("This search has expired, please search again.")
        return

    await query.edit_message_reply_markup(
        reply_markup=results_page_markup(search_id, docs, page=int(page_str))
    )


async def send_movie_details(reply_text, reply_photo, client, identifier: str) -> None:
    try:
        details = await get_movie_details(client, identifier)
    except httpx.HTTPError as e:
        log.warning("metadata fetch failed: %s", e)
        await reply_text("Couldn't fetch that item, please try another.")
        return

    if not details:
        await reply_text("No verified public-domain video file found for that item.")
        return

    size_str = f"{details['size_mb']:.0f} MB" if details["size_mb"] else "unknown size"
    year_str = f" ({details['year']})" if details["year"] else ""
    caption = (
        f"🎬 *{html.escape(details['title'])}{year_str}*\n\n"
        f"{html.escape(details['description'])}\n\n"
        f"[Direct download link]({details['download_url']}) ({size_str})\n"
        f"[Item page]({details['item_url']})\n\n"
        "Source: Internet Archive — public domain / openly licensed."
    )

    try:
        await reply_photo(
            details["thumbnail_url"], caption=caption, parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        await reply_text(caption, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=False)


async def handle_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    _, search_id, idx_str = query.data.split(":", 2)
    docs = context.user_data.get("searches", {}).get(search_id)
    if docs is None or int(idx_str) >= len(docs):
        await query.edit_message_text("This search has expired, please search again.")
        return

    identifier = docs[int(idx_str)]["identifier"]
    client: httpx.AsyncClient = context.bot_data["http_client"]

    await send_movie_details(
        lambda *a, **kw: query.message.reply_text(*a, **kw),
        lambda *a, **kw: query.message.reply_photo(*a, **kw),
        client,
        identifier,
    )


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


async def post_init(application: Application) -> None:
    application.bot_data["http_client"] = httpx.AsyncClient()


async def post_shutdown(application: Application) -> None:
    client: httpx.AsyncClient = application.bot_data.get("http_client")
    if client:
        await client.aclose()


def build_application() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("random", random_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    app.add_handler(CallbackQueryHandler(handle_page, pattern=r"^pg:"))
    app.add_handler(CallbackQueryHandler(handle_selection, pattern=r"^get:"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
    return app


async def run_webhook(app: Application) -> None:
    """Custom aiohttp server instead of PTB's built-in one, so we can add a
    health-check route — Render's default health check hits `/`, and PTB's
    own webhook server 404s on anything but the token path."""

    async def telegram_webhook(request: web.Request) -> web.Response:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return web.Response(status=401)
        update = Update.de_json(await request.json(), app.bot)
        await app.update_queue.put(update)
        return web.Response()

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    web_app = web.Application()
    web_app.router.add_post(f"/{BOT_TOKEN}", telegram_webhook)
    web_app.router.add_get("/", health)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)

    async with app:
        # `async with app` only calls Application.initialize(), which explicitly
        # does NOT run post_init/post_shutdown — those are normally invoked by
        # PTB's own run_polling()/run_webhook(), which this custom server bypasses.
        await post_init(app)
        try:
            await app.bot.set_webhook(
                url=f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}",
                secret_token=WEBHOOK_SECRET,
            )
            await app.start()
            await site.start()
            log.info("Webhook server listening on port %s", PORT)
            try:
                await asyncio.Event().wait()  # run forever, until cancelled
            except asyncio.CancelledError:
                pass
            finally:
                await runner.cleanup()
                await app.stop()
        finally:
            await post_shutdown(app)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN in the environment (see .env.example)."
        )

    app = build_application()

    if WEBHOOK_URL:
        log.info("Bot starting in webhook mode on port %s...", PORT)
        asyncio.run(run_webhook(app))
    else:
        log.info("Bot starting in polling mode...")
        app.run_polling()


if __name__ == "__main__":
    main()
