#!/usr/bin/env python3
"""Telegram bot: pick a hololive member -> source -> tag, fetch via existing workers, send, delete."""
import asyncio, importlib, json, os, re, shutil, tempfile, threading
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

import shared
from bot_data import MEMBERS, TAGS

TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "8080"))
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(_BOT_DIR, "bot_users.json")
# vendored shared.py writes each download to database/gallery.json; the bot never reads it
os.makedirs(os.path.join(_BOT_DIR, "database"), exist_ok=True)

DEFAULT_AMOUNT = int(os.getenv("BOT_AMOUNT", "5"))

LANG_EMOJI = {"jp": "🇯🇵", "id": "🇮🇩", "en": "🇺🇸"}
BRANCH_LABEL = {"hololive": "hololive", "holostars": "HOLOSTARS"}
PAGE_SIZE = 10

# source key -> (label, report_key, needs_creds)
# sources = workers with real hololive member tags in bot_data (from the report)
SOURCES = [
    ("yande", "Yande.re", "yande.re", False),
    ("kona", "Konachan", "konachan", False),
    ("dan", "Danbooru", "danbooru", True),
    ("safe", "Safebooru", "safebooru", False),
    ("zero", "Zerochan", "zerochan", False),
    ("nekosia", "Nekosia", "nekosia", False),
    ("eshuushuu", "E-shuushuu", "eshuushuu", False),
    ("anime_dl", "Anime Pictures", "anime_dl", False),
]
SOURCE_BY_REPORT = {r: k for k, _l, r, _c in SOURCES if r}

# worker dispatch, mirrors Rem_catcher._DISPATCH but proxy-free; tag + net_config in, full args out
FETCHERS = {
    "yande": ("workers.yande", "worker_yande", lambda t, nc, n: (t, n, "", nc)),
    "kona": ("workers.konachan", "worker_konachan", lambda t, nc, n: (t, n, "", [], nc)),
    "dan": ("workers.danbooru", "worker_danbooru", lambda t, nc, n: (t, n, "", [], nc)),
    "safe": ("workers.safebooru", "worker_safebooru", lambda t, nc, n: (t, n, [], nc)),
    "zero": ("workers.zerochan", "worker_zerochan", lambda t, nc, n: (t, n, nc)),
    "nekosia": ("workers.nekosia", "worker_nekosia", lambda t, nc, n: (t, n, nc)),
    "eshuushuu": ("workers.eshuushuu", "worker_eshuushuu", lambda t, nc, n: (t, n, [], "", nc)),
    "anime_dl": ("workers.anime_dl", "worker_anime_dl", lambda t, nc, n: (t, n, nc)),
}

# ── per-user credential store ──────────────────────────────────
_LOCK = threading.Lock()
def _load_users():
    with _LOCK:
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

def _save_users(data):
    with _LOCK:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

def user_creds(uid):
    return _load_users().get(str(uid), {})

def set_user_creds(uid, source, fields):
    data = _load_users()
    data.setdefault(str(uid), {})[source] = fields
    _save_users(data)

def user_pref(uid, key, default):
    return _load_users().get(str(uid), {}).get("_prefs", {}).get(key, default)

def set_user_pref(uid, key, value):
    data = _load_users()
    data.setdefault(str(uid), {}).setdefault("_prefs", {})[key] = value
    _save_users(data)

# ── menu state (short ids -> full state, callback_data ≤64 bytes) ──
_MENU = {}
_MENU_ID = [0]
def _store(state):
    _MENU_ID[0] += 1
    kid = f"{_MENU_ID[0]:x}"
    _MENU[kid] = state
    if len(_MENU) > 2000:
        for k in list(_MENU)[:500]:
            _MENU.pop(k, None)
    return kid

def _get(kid):
    return _MENU.get(kid)

# ── keyboard builders ──────────────────────────────────────────
def branch_keyboard():
    rows = []
    for lang in ("jp", "id", "en"):
        for br in ("hololive", "holostars"):
            n = sum(1 for m in MEMBERS.values() if m["lang"] == lang and m["branch"] == br)
            if n:
                rows.append([InlineKeyboardButton(f"{LANG_EMOJI[lang]} {BRANCH_LABEL[br]} ({n})",
                                                  callback_data=f"b:{lang}:{br}")])
    rows.append([InlineKeyboardButton(f"All ({len(MEMBERS)})", callback_data="b:all")])
    return InlineKeyboardMarkup(rows)

def member_keyboard(members, page):
    keys = []
    for name in members[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        m = MEMBERS[name]
        keys.append([InlineKeyboardButton(f"{LANG_EMOJI[m['lang']]} {name} ({m['gen']})",
                                          callback_data=f"m:{_store({'name': name})}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"pg:{_store({'members': members, 'page': page - 1})}"))
    nav.append(InlineKeyboardButton("Back", callback_data=f"branch:{_store({'branch': 'home'})}"))
    if (page + 1) * PAGE_SIZE < len(members):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"pg:{_store({'members': members, 'page': page + 1})}"))
    keys.append(nav)
    return InlineKeyboardMarkup(keys)

def available_sources(name):
    """Sources that have real tag data for this member."""
    tl = TAGS.get(name, {})
    return [(skey, label) for skey, label, report_key, _needs in SOURCES
            if any(tl[r] for r in tl if SOURCE_BY_REPORT.get(r) == skey)]

def source_keyboard(name, page=0):
    sources = available_sources(name)
    keys = []
    for skey, label in sources[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        keys.append([InlineKeyboardButton(label, callback_data=f"s:{_store({'name': name, 'source': skey})}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"pg:{_store({'name': name, 'back': 'sources', 'page': page - 1})}"))
    nav.append(InlineKeyboardButton("Back", callback_data=f"branch:{_store({'branch': 'home'})}"))
    if (page + 1) * PAGE_SIZE < len(sources):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"pg:{_store({'name': name, 'back': 'sources', 'page': page + 1})}"))
    keys.append(nav)
    return InlineKeyboardMarkup(keys)

def _tag_sort_key(tag, name):
    """Natural sort: base name first, then costume numbers 1,2,...,10, then the rest."""
    base = re.sub(r"\W", "", name.lower())
    t = re.sub(r"\W", "", tag.lower())
    m = re.search(r"(\d+)(?:st|nd|rd|th)costume", t)
    if t == base:
        return (0, 0, "")
    if m:
        return (1, int(m.group(1)), "")
    return (2, 0, tag.lower())

def member_tags(name, skey):
    report_key = next((r for k, _l, r, _c in SOURCES if k == skey), None)
    if report_key and name in TAGS and report_key in TAGS[name]:
        return sorted(TAGS[name][report_key], key=lambda t: _tag_sort_key(t, name))
    return []

def tag_keyboard(name, skey, page=0):
    tl = member_tags(name, skey)
    keys = []
    for t in tl[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        keys.append([InlineKeyboardButton(t, callback_data=f"t:{_store({'name': name, 'source': skey, 'tag': t})}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"pg:{_store({'name': name, 'source': skey, 'back': 'tags', 'page': page - 1})}"))
    nav.append(InlineKeyboardButton("Back", callback_data=f"s:{_store({'name': name, 'source': skey})}"))
    if (page + 1) * PAGE_SIZE < len(tl):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"pg:{_store({'name': name, 'source': skey, 'back': 'tags', 'page': page + 1})}"))
    keys.append(nav)
    return InlineKeyboardMarkup(keys), None

async def prompt_creds(update, context, skey):
    """Ask a user for API credentials via their private chat with the bot."""
    q = update.callback_query
    bot_username = context.bot.username
    fields = " ".join(KEY_FIELDS[skey][1]) if skey in KEY_FIELDS else "values"
    url = f"https://t.me/{bot_username}" if bot_username else None
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open private chat", url=url)]]) if url else None
    await q.edit_message_text(
        f"{skey} needs your API key.\n"
        f"Send **/setkey {skey} {fields}** in a private chat with me — never in the group.",
        reply_markup=kb)
    try:
        await context.bot.send_message(
            q.from_user.id,
            f"You tried to fetch from **{skey}**.\nSend me **/setkey {skey} {fields}** here to add your API key.\n"
            f"Only you can use your key; it's never shared with other users.")
    except Exception:
        pass  # user hasn't started the bot — the group prompt still shows

COUNT_CHOICES = [1, 2, 3, 5, 10, 20]

def count_keyboard(name, skey, tag, uid):
    rows = []
    for i in range(0, len(COUNT_CHOICES), 3):
        rows.append([InlineKeyboardButton(str(n), callback_data=f"count:{_store({'name': name, 'source': skey, 'tag': tag, 'count': n})}")
                     for n in COUNT_CHOICES[i:i+3]])
    as_doc = user_pref(uid, "as_doc", False)
    mode_label = "Send as file (full res)" if not as_doc else "Send as photo (HD)"
    rows.append([InlineKeyboardButton(f"📄 {mode_label}", callback_data=f"mode:{_store({'name': name, 'source': skey, 'tag': tag, 'back': 'count'})}")])
    rows.append([InlineKeyboardButton("Cancel", callback_data=f"branch:{_store({'branch': 'home'})}")])
    return InlineKeyboardMarkup(rows)

def confirm_keyboard(name, skey, tag, count):
    label = next((l for k, l, _r, _c in SOURCES if k == skey), skey)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"Fetch {count} from {label}", callback_data=f"fetch:{_store({'name': name, 'source': skey, 'tag': tag, 'count': count})}"),
        InlineKeyboardButton("Cancel", callback_data=f"branch:{_store({'branch': 'home'})}"),
    ]])

# ── menu handlers ──────────────────────────────────────────────
async def cmd_menu(update, context):
    await update.effective_chat.send_message("Choose a branch:", reply_markup=branch_keyboard())

async def cmd_start(update, context):
    await update.effective_chat.send_message(
        "I fetch images for hololive members.\nTap me in a group or use /menu, /members, /tags, /sources.")

async def cmd_members(update, context):
    args = context.args
    if args and args[0].lower() in ("jp", "id", "en"):
        lang = args[0].lower()
        members = [n for n, m in MEMBERS.items() if m["lang"] == lang]
    else:
        members = list(MEMBERS)
    members.sort(key=lambda n: (MEMBERS[n]["branch"], MEMBERS[n]["lang"]))
    await update.effective_chat.send_message(f"Members ({len(members)}):", reply_markup=member_keyboard(members, 0))

async def cmd_tags(update, context):
    q = " ".join(context.args).strip().lower()
    if not q:
        await update.effective_chat.send_message("Usage: /tags <member name>")
        return
    name = next((n for n in MEMBERS if q in n.lower()), None)
    if not name:
        await update.effective_chat.send_message("No member found.")
        return
    lines = [f"Tags for {name}:"]
    for skey, label, _r, _c in SOURCES:
        tl = member_tags(name, skey)
        if tl:
            lines.append(f"• {label}: {', '.join(tl[:6])}{'…' if len(tl) > 6 else ''}")
    await update.effective_chat.send_message("\n".join(lines))

async def cmd_sources(update, context):
    uid = str(update.effective_user.id)
    creds = user_creds(uid)
    lines = ["Sources:"]
    for skey, label, _r, needs in SOURCES:
        c = "✅" if needs and skey in creds else ("—" if needs else "no auth")
        lines.append(f"• {label} [{c}]")
    lines.append("\nSet keys privately with /setkey (e.g. /setkey gelbooru <api_key> <user_id>) — DM the bot.")
    await update.effective_chat.send_message("\n".join(lines))

KEY_FIELDS = {
    "danbooru": ("danbooru", ["login", "api_key"]),
}

async def cmd_setkey(update, context):
    uid = str(update.effective_user.id)
    if update.effective_chat.type != "private":
        await update.effective_chat.send_message(
            "I only accept API keys in a private chat — a key sent in a group would be visible to everyone.\n"
            "DM me and use /setkey there.")
        return
    args = context.args
    if not args or args[0].lower() not in KEY_FIELDS:
        await update.effective_chat.send_message(
            "Usage: /setkey <source> <values…>\nSources: " + ", ".join(KEY_FIELDS))
        return
    skey = args[0].lower()
    _, fields = KEY_FIELDS[skey]
    vals = args[1:]
    if len(vals) < len(fields):
        await update.effective_chat.send_message(f"Need {len(fields)} values: {' '.join(fields)}")
        return
    set_user_creds(uid, skey, dict(zip(fields, vals)))
    await update.effective_chat.send_message(f"Saved credentials for {skey} (only you can use them).")

async def cmd_mykeys(update, context):
    uid = str(update.effective_user.id)
    creds = user_creds(uid)
    if not creds:
        await update.effective_chat.send_message("No saved keys.")
        return
    mask = {k: {f: ("*" * 6) if "key" in f or "password" in f or "token" in f else v for f, v in fields.items()}
            for k, fields in creds.items()}
    await update.effective_chat.send_message("\n".join(f"{k}: {mask[k]}" for k in creds))

async def cmd_mode(update, context):
    """Toggle send-as-document (full resolution) vs compressed photo."""
    uid = str(update.effective_user.id)
    args = context.args
    if args and args[0].lower() in ("photo", "hd", "0"):
        set_user_pref(uid, "as_doc", False)
        await update.effective_chat.send_message("Send mode: **photo (HD)** — images are shown inline.")
    elif args and args[0].lower() in ("doc", "file", "document", "1"):
        set_user_pref(uid, "as_doc", True)
        await update.effective_chat.send_message("Send mode: **document** — images sent as files at full resolution.")
    else:
        cur = "document (full res)" if user_pref(uid, "as_doc", False) else "photo (HD)"
        await update.effective_chat.send_message(
            f"Current send mode: **{cur}**\n"
            "Use /mode doc to send as files (full resolution), or /mode photo for HD inline images.")

async def on_menu_click(update, context):
    q = update.callback_query
    data = q.data
    await q.answer()
    parts = data.split(":", 1)
    kind = parts[0]

    if kind == "b":
        _, rest = parts
        if rest == "all":
            members = list(MEMBERS)
            title = f"All members ({len(members)}):"
        else:
            lang, br = rest.split(":")
            members = [n for n, m in MEMBERS.items() if m["lang"] == lang and m["branch"] == br]
            title = f"{LANG_EMOJI[lang]} {BRANCH_LABEL[br]} ({len(members)}):"
        members.sort(key=lambda n: (MEMBERS[n]["gen"], MEMBERS[n]["lang"]))
        await q.edit_message_text(title, reply_markup=member_keyboard(members, 0))

    elif kind == "branch":
        await q.edit_message_text("Choose a branch:", reply_markup=branch_keyboard())

    elif kind == "m":
        st = _get(parts[1])
        name = st["name"]
        if not available_sources(name):
            await q.edit_message_text(f"No tag data for {name} in any source yet.")
            return
        await q.edit_message_text(f"{name} — source:", reply_markup=source_keyboard(name, 0))

    elif kind == "s":
        st = _get(parts[1])
        name, skey = st["name"], st["source"]
        needs = next((c for k, _l, _r, c in SOURCES if k == skey), False)
        if needs and skey not in user_creds(str(q.from_user.id)):
            await prompt_creds(update, context, skey)
            return
        kb, note = tag_keyboard(name, skey, 0)
        text = f"{name} — {next(l for k, l, _r, _c in SOURCES if k == skey)} — tag:" + (f"\n{note}" if note else "")
        await q.edit_message_text(text, reply_markup=kb)

    elif kind == "t":
        st = _get(parts[1])
        name, skey, tag = st["name"], st["source"], st["tag"]
        # if source needs creds and user has none, gate here
        uid = str(q.from_user.id)
        needs = next((c for k, _l, _r, c in SOURCES if k == skey), False)
        if needs and skey not in user_creds(uid):
            await prompt_creds(update, context, skey)
            return
        await q.edit_message_text(f"{name} • {tag} — how many?", reply_markup=count_keyboard(name, skey, tag, uid))

    elif kind == "pg":
        st = _get(parts[1])
        page = st.get("page", 0)
        if "members" in st:
            await q.edit_message_text("Members:", reply_markup=member_keyboard(st["members"], page))
        elif st.get("back") == "sources":
            name = st["name"]
            await q.edit_message_text(f"{name} — source:", reply_markup=source_keyboard(name, page))
        elif st.get("back") == "tags":
            name, skey = st["name"], st["source"]
            kb, _note = tag_keyboard(name, skey, page)
            await q.edit_message_text(f"{name} — tag:", reply_markup=kb)

    elif kind == "mode":
        st = _get(parts[1])
        name, skey, tag = st["name"], st["source"], st["tag"]
        uid = str(q.from_user.id)
        cur = user_pref(uid, "as_doc", False)
        set_user_pref(uid, "as_doc", not cur)
        await q.edit_message_text(f"{name} • {tag} — how many?",
                                  reply_markup=count_keyboard(name, skey, tag, uid))

    elif kind == "count":
        st = _get(parts[1])
        name, skey, tag, count = st["name"], st["source"], st["tag"], st["count"]
        uid = str(q.from_user.id)
        await q.edit_message_text(f"Fetch {count} of {name} from {skey} ({tag})?",
                                  reply_markup=confirm_keyboard(name, skey, tag, count))

    elif kind == "fetch":
        st = _get(parts[1])
        name, skey, tag, count = st["name"], st["source"], st["tag"], st.get("count", DEFAULT_AMOUNT)
        uid = str(q.from_user.id)
        await q.edit_message_text(f"Fetching {count} for {name} from {skey}… ({tag})")
        await context.application.create_task(_fetch_and_send(update.effective_chat.id, uid, name, skey, tag, count))

# ── fetch + send ───────────────────────────────────────────────
_FETCH_LOCK = threading.Lock()
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif"}

def _run_worker(skey, tag, net_config, count):
    mod_name, fn_name, extractor = FETCHERS[skey]
    mod = importlib.import_module(mod_name)
    fn = getattr(mod, fn_name)
    args = extractor(tag, net_config, count)
    fn(*args)

async def _fetch_and_send(chat_id, uid, name, skey, tag, count):
    label = next(l for k, l, _r, _c in SOURCES if k == skey)
    try:
        with _FETCH_LOCK:
            creds = user_creds(uid)
            net_config = {"use_proxy": False, "verify_tls": False, "anti_ban_pause": 1.0,
                          "api_timeout": 10, "retry_wait": 3, "download_retries": 1}
            net_config.update(creds.get(skey, {}))

            tmp = tempfile.mkdtemp(prefix="remgod_bot_")
            old = shared.MASTER_FOLDER
            shared.MASTER_FOLDER = tmp
            try:
                await asyncio.to_thread(_run_worker, skey, tag, net_config, count)
            finally:
                shared.MASTER_FOLDER = old

            files = []
            for root, _d, names in os.walk(tmp):
                for n in names:
                    if os.path.splitext(n)[1].lower() in IMAGE_EXT:
                        files.append(os.path.join(root, n))

        if not files:
            await _app.bot.send_message(chat_id, f"No images found for {name} on {label} ({tag}).")
            return
        as_doc = user_pref(uid, "as_doc", False)
        await _app.bot.send_message(chat_id, f"Found {len(files)} — sending… ({'files' if as_doc else 'photos'})")
        for fp in files[:count]:
            try:
                with open(fp, "rb") as fh:
                    if as_doc or os.path.splitext(fp)[1].lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                        await _app.bot.send_document(chat_id, fh, caption=f"{name} • {label} • {tag}")
                    else:
                        await _app.bot.send_photo(chat_id, fh, caption=f"{name} • {label} • {tag}")
            except Exception as e:
                await _app.bot.send_message(chat_id, f"Send failed for one file: {e}")
    except Exception as e:
        await _app.bot.send_message(chat_id, f"Error fetching {name} from {label}: {e}")
    finally:
        if "tmp" in dir():
            shutil.rmtree(tmp, ignore_errors=True)

# ── mention trigger ────────────────────────────────────────────
async def on_mention(update, context):
    if not update.effective_message:
        return
    text = update.effective_message.text or ""
    if context.bot.username and f"@{context.bot.username}" in text:
        await cmd_menu(update, context)

# ── main ───────────────────────────────────────────────────────
def _serve_health(port):
    """Minimal HTTP 200 server so Railway's healthcheck passes (long-polling needs no webhook)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("0.0.0.0", port), _Health)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN not set")
    app = Application.builder().token(TOKEN).build()
    global _app
    _app = app

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("members", cmd_members))
    app.add_handler(CommandHandler("tags", cmd_tags))
    app.add_handler(CommandHandler("sources", cmd_sources))
    app.add_handler(CommandHandler("setkey", cmd_setkey))
    app.add_handler(CommandHandler("mykeys", cmd_mykeys))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CallbackQueryHandler(on_menu_click))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_mention))

    _serve_health(PORT)
    app.run_polling()

if __name__ == "__main__":
    main()
