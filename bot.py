#!/usr/bin/env python3
"""Telegram bot: pick a hololive member -> source -> tag, fetch via existing workers, send, delete."""
import asyncio, importlib, json, os, re, shutil, tempfile, threading
from dotenv import load_dotenv; load_dotenv()

import shared
from bot_data import MEMBERS, TAGS

from aiogram import Dispatcher, Bot, types, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import BotCommand, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "8080"))
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(_BOT_DIR, "bot_users.json")
os.makedirs(os.path.join(_BOT_DIR, "database"), exist_ok=True)

DEFAULT_AMOUNT = int(os.getenv("BOT_AMOUNT", "5"))

LANG_EMOJI = {"jp": "🇯🇵", "id": "🇮🇩", "en": "🇺🇸"}
BRANCH_LABEL = {"hololive": "hololive", "holostars": "HOLOSTARS"}
PAGE_SIZE = 10

# (key, label, report_key)
SOURCES = [
    ("yande", "Yande.re", "yande.re"),
    ("kona", "Konachan", "konachan"),
    ("dan", "Danbooru", "danbooru"),
    ("safe", "Safebooru", "safebooru"),
    ("zero", "Zerochan", "zerochan"),
    ("nekosia", "Nekosia", "nekosia"),
    ("eshuushuu", "E-shuushuu", "eshuushuu"),
    ("anime_dl", "Anime Pictures", "anime_dl"),
]
SOURCE_BY_REPORT = {r: k for k, _l, r in SOURCES}

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

# ── per-user prefs (send mode only) ──────────────────────
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

def user_pref(uid, key, default):
    return _load_users().get(str(uid), {}).get("_prefs", {}).get(key, default)

def set_user_pref(uid, key, value):
    data = _load_users()
    data.setdefault(str(uid), {}).setdefault("_prefs", {})[key] = value
    _save_users(data)

# ── menu state ───────────────────────────────────────────
_MENU = {}
_MENU_ID = [0]
_COUNT_PENDING = {}
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

def _kb(rows):
    from aiogram.types import InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _btn(text, data=None, url=None):
    from aiogram.types import InlineKeyboardButton
    kwargs = {"text": text}
    if data is not None:
        kwargs["callback_data"] = data
    if url is not None:
        kwargs["url"] = url
    return InlineKeyboardButton(**kwargs)

def branch_keyboard():
    rows = []
    for lang in ("jp", "id", "en"):
        for br in ("hololive", "holostars"):
            n = sum(1 for m in MEMBERS.values() if m["lang"] == lang and m["branch"] == br)
            if n:
                rows.append([_btn(f"{LANG_EMOJI[lang]} {BRANCH_LABEL[br]} ({n})", f"b:{lang}:{br}")])
    rows.append([_btn(f"All ({len(MEMBERS)})", "b:all")])
    return _kb(rows)

def member_keyboard(members, page):
    keys = []
    for name in members[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        m = MEMBERS[name]
        keys.append([_btn(f"{LANG_EMOJI[m['lang']]} {name} ({m['gen']})", f"m:{_store({'name': name})}")])
    nav = []
    if page > 0:
        nav.append(_btn("◀️", f"pg:{_store({'members': members, 'page': page - 1})}"))
    nav.append(_btn("Back", f"branch:{_store({'branch': 'home'})}"))
    if (page + 1) * PAGE_SIZE < len(members):
        nav.append(_btn("▶️", f"pg:{_store({'members': members, 'page': page + 1})}"))
    keys.append(nav)
    return _kb(keys)

def available_sources(name):
    tl = TAGS.get(name, {})
    return [(skey, label) for skey, label, report_key in SOURCES
            if any(tl[r] for r in tl if SOURCE_BY_REPORT.get(r) == skey)]

def source_keyboard(name, page=0):
    sources = available_sources(name)
    keys = []
    for skey, label in sources[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        keys.append([_btn(label, f"s:{_store({'name': name, 'source': skey})}")])
    nav = []
    if page > 0:
        nav.append(_btn("◀️", f"pg:{_store({'name': name, 'back': 'sources', 'page': page - 1})}"))
    nav.append(_btn("Back", f"branch:{_store({'branch': 'home'})}"))
    if (page + 1) * PAGE_SIZE < len(sources):
        nav.append(_btn("▶️", f"pg:{_store({'name': name, 'back': 'sources', 'page': page + 1})}"))
    keys.append(nav)
    return _kb(keys)

def _tag_sort_key(tag, name):
    base = re.sub(r"\W", "", name.lower())
    t = re.sub(r"\W", "", tag.lower())
    m = re.search(r"(\d+)(?:st|nd|rd|th)costume", t)
    if t == base:
        return (0, 0, "")
    if m:
        return (1, int(m.group(1)), "")
    return (2, 0, tag.lower())

def member_tags(name, skey):
    report_key = next((r for k, _l, r in SOURCES if k == skey), None)
    if report_key and name in TAGS and report_key in TAGS[name]:
        return sorted(TAGS[name][report_key], key=lambda t: _tag_sort_key(t, name))
    return []

def tag_keyboard(name, skey, page=0):
    tl = member_tags(name, skey)
    keys = []
    for t in tl[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        keys.append([_btn(t, f"t:{_store({'name': name, 'source': skey, 'tag': t})}")])
    nav = []
    if page > 0:
        nav.append(_btn("◀️", f"pg:{_store({'name': name, 'source': skey, 'back': 'tags', 'page': page - 1})}"))
    nav.append(_btn("Back", f"bk:{_store({'to': 'sources', 'name': name})}"))
    if (page + 1) * PAGE_SIZE < len(tl):
        nav.append(_btn("▶️", f"pg:{_store({'name': name, 'source': skey, 'back': 'tags', 'page': page + 1})}"))
    keys.append(nav)
    return _kb(keys), None

COUNT_CHOICES = [1, 2, 3, 5, 10, 20]

def count_keyboard(name, skey, tag, uid):
    rows = []
    for i in range(0, len(COUNT_CHOICES), 3):
        rows.append([_btn(str(n), f"count:{_store({'name': name, 'source': skey, 'tag': tag, 'count': n})}")
                     for n in COUNT_CHOICES[i:i+3]])
    as_doc = user_pref(uid, "as_doc", False)
    mode_label = "Send as file (full res)" if not as_doc else "Send as photo (HD)"
    rows.append([_btn(f"📄 {mode_label}", f"mode:{_store({'name': name, 'source': skey, 'tag': tag, 'back': 'count'})}")])
    rows.append([_btn("Back", f"bk:{_store({'to': 'tags', 'name': name, 'source': skey})}")])
    return _kb(rows)

def confirm_keyboard(name, skey, tag, count):
    label = next((l for k, l, _r in SOURCES if k == skey), skey)
    return _kb([[
        _btn(f"Fetch {count} from {label}", f"fetch:{_store({'name': name, 'source': skey, 'tag': tag, 'count': count})}"),
        _btn("Back", f"bk:{_store({'to': 'count', 'name': name, 'source': skey, 'tag': tag})}"),
    ]])

# ── commands ──────────────────────────────────────────────
async def cmd_menu(message: types.Message, **kw):
    await message.answer("Choose a branch:", reply_markup=branch_keyboard())

async def cmd_start(message: types.Message, **kw):
    await message.answer("I fetch images for hololive members.\nTap me in a group or use /menu, /members, /tags, /sources.")

async def cmd_members(message: types.Message, **kw):
    text = message.text or ""
    args = text.split(maxsplit=1)[1].split() if len(text.split()) > 1 else []
    if args and args[0].lower() in ("jp", "id", "en"):
        lang = args[0].lower()
        members = [n for n, m in MEMBERS.items() if m["lang"] == lang]
    else:
        members = list(MEMBERS)
    members.sort(key=lambda n: (MEMBERS[n]["branch"], MEMBERS[n]["lang"]))
    await message.answer(f"Members ({len(members)}):", reply_markup=member_keyboard(members, 0))

async def cmd_tags(message: types.Message, **kw):
    text = message.text or ""
    q = " ".join(text.split()[1:]).strip().lower()
    if not q:
        await message.answer("Usage: /tags <member name>")
        return
    name = next((n for n in MEMBERS if q in n.lower()), None)
    if not name:
        await message.answer("No member found.")
        return
    lines = [f"Tags for {name}:"]
    for skey, label, _r in SOURCES:
        tl = member_tags(name, skey)
        if tl:
            lines.append(f"• {label}: {', '.join(tl[:6])}{'…' if len(tl) > 6 else ''}")
    await message.answer("\n".join(lines))

async def cmd_sources(message: types.Message, **kw):
    lines = ["Sources:"]
    for skey, label, _r in SOURCES:
        lines.append(f"• {label}")
    await message.answer("\n".join(lines))

async def cmd_mode(message: types.Message, **kw):
    uid = str(message.from_user.id)
    text = message.text or ""
    args = text.split()[1:]
    if args and args[0].lower() in ("photo", "hd", "0"):
        set_user_pref(uid, "as_doc", False)
        await message.answer("Send mode: **photo (HD)** — images are shown inline.")
    elif args and args[0].lower() in ("doc", "file", "document", "1"):
        set_user_pref(uid, "as_doc", True)
        await message.answer("Send mode: **document** — images sent as files at full resolution.")
    else:
        cur = "document (full res)" if user_pref(uid, "as_doc", False) else "photo (HD)"
        await message.answer(
            f"Current send mode: **{cur}**\n"
            "Use /mode doc to send as files (full resolution), or /mode photo for HD inline images.")

# ── fetch + send ──────────────────────────────────────────
_FETCH_LOCK = threading.Lock()
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif"}

def _run_worker(skey, tag, net_config, count):
    mod_name, fn_name, extractor = FETCHERS[skey]
    mod = importlib.import_module(mod_name)
    fn = getattr(mod, fn_name)
    args = extractor(tag, net_config, count)
    fn(*args)

async def _fetch_and_send(chat_id, uid, name, skey, tag, count, bot: Bot):
    label = next(l for k, l, _r in SOURCES if k == skey)
    try:
        with _FETCH_LOCK:
            proxy = os.getenv("https_proxy") or os.getenv("http_proxy")
            net_config = {"use_proxy": bool(proxy), "proxy_url": proxy or "",
                          "verify_tls": False, "anti_ban_pause": 1.0,
                          "api_timeout": 10, "retry_wait": 3, "download_retries": 1}

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
            await bot.send_message(chat_id, f"No images found for {name} on {label} ({tag}).")
            return
        as_doc = user_pref(uid, "as_doc", False)
        await bot.send_message(chat_id, f"Found {len(files)} — sending… ({'files' if as_doc else 'photos'})")
        for fp in files[:count]:
            try:
                fsize = os.path.getsize(fp)
                use_doc = as_doc or fsize > 10*1024*1024 or os.path.splitext(fp)[1].lower() not in {".jpg", ".jpeg", ".png", ".webp"}
                infile = FSInputFile(fp)
                if use_doc:
                    await asyncio.wait_for(bot.send_document(chat_id, infile, caption=f"{name} • {label} • {tag}"), timeout=30)
                else:
                    await asyncio.wait_for(bot.send_photo(chat_id, infile, caption=f"{name} • {label} • {tag}"), timeout=30)
            except asyncio.TimeoutError:
                await bot.send_message(chat_id, f"Send timeout for {name} • {label} • {tag} - try smaller amount")
            except Exception as e:
                await bot.send_message(chat_id, f"Send failed for one file: {e}")
    except Exception as e:
        await bot.send_message(chat_id, f"Error fetching {name} from {label}: {e}")
    finally:
        if "tmp" in dir():
            shutil.rmtree(tmp, ignore_errors=True)

# ── callback handler ──────────────────────────────────────
async def on_menu_click(call: types.CallbackQuery, bot: Bot):
    data = call.data
    await call.answer()
    parts = data.split(":", 1)
    kind = parts[0]
    uid = str(call.from_user.id)
    if kind in ("b", "branch", "m", "s", "pg", "bk"):
        _COUNT_PENDING.pop(uid, None)

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
        await call.message.edit_text(title, reply_markup=member_keyboard(members, 0))

    elif kind == "branch":
        await call.message.edit_text("Choose a branch:", reply_markup=branch_keyboard())

    elif kind == "bk":
        st = _get(parts[1])
        to = st["to"]
        if to == "sources":
            name = st["name"]
            await call.message.edit_text(f"{name} — source:", reply_markup=source_keyboard(name, 0))
        elif to == "tags":
            name, skey = st["name"], st["source"]
            kb, _note = tag_keyboard(name, skey, 0)
            await call.message.edit_text(f"{name} — tag:", reply_markup=kb)
        elif to == "count":
            name, skey, tag = st["name"], st["source"], st["tag"]
            await call.message.edit_text(f"{name} • {tag} — how many?",
                                         reply_markup=count_keyboard(name, skey, tag, uid))

    elif kind == "m":
        st = _get(parts[1])
        name = st["name"]
        if not available_sources(name):
            await call.message.edit_text(f"No tag data for {name} in any source yet.")
            return
        await call.message.edit_text(f"{name} — source:", reply_markup=source_keyboard(name, 0))

    elif kind == "s":
        st = _get(parts[1])
        name, skey = st["name"], st["source"]
        kb, note = tag_keyboard(name, skey, 0)
        text = f"{name} — {next(l for k, l, _r in SOURCES if k == skey)} — tag:" + (f"\n{note}" if note else "")
        await call.message.edit_text(text, reply_markup=kb)

    elif kind == "t":
        st = _get(parts[1])
        name, skey, tag = st["name"], st["source"], st["tag"]
        _COUNT_PENDING[uid] = {"name": name, "source": skey, "tag": tag}
        await call.message.edit_text(f"{name} • {tag} — how many?", reply_markup=count_keyboard(name, skey, tag, uid))

    elif kind == "pg":
        st = _get(parts[1])
        page = st.get("page", 0)
        if "members" in st:
            await call.message.edit_text("Members:", reply_markup=member_keyboard(st["members"], page))
        elif st.get("back") == "sources":
            name = st["name"]
            await call.message.edit_text(f"{name} — source:", reply_markup=source_keyboard(name, page))
        elif st.get("back") == "tags":
            name, skey = st["name"], st["source"]
            kb, _note = tag_keyboard(name, skey, page)
            await call.message.edit_text(f"{name} — tag:", reply_markup=kb)

    elif kind == "mode":
        st = _get(parts[1])
        name, skey, tag = st["name"], st["source"], st["tag"]
        cur = user_pref(uid, "as_doc", False)
        set_user_pref(uid, "as_doc", not cur)
        await call.message.edit_text(f"{name} • {tag} — how many?",
                                  reply_markup=count_keyboard(name, skey, tag, uid))

    elif kind == "count":
        st = _get(parts[1])
        name, skey, tag, count = st["name"], st["source"], st["tag"], st["count"]
        _COUNT_PENDING[uid] = st
        await call.message.edit_text(f"Fetch {count} of {name} from {skey} ({tag})? (or type a number to change)",
                                  reply_markup=confirm_keyboard(name, skey, tag, count))

    elif kind == "fetch":
        st = _get(parts[1])
        name, skey, tag, count = st["name"], st["source"], st["tag"], st.get("count", DEFAULT_AMOUNT)
        _COUNT_PENDING.pop(uid, None)
        await call.message.edit_text(f"Fetching {count} for {name} from {skey}… ({tag})")
        asyncio.create_task(_fetch_and_send(call.message.chat.id, uid, name, skey, tag, count, bot))

# ── message handler ──────────────────────────────────────
_BOT_USERNAME = ""

async def on_message(message: types.Message, bot: Bot):
    if not message.text:
        return
    text = message.text.strip()
    uid = str(message.from_user.id)

    st = _COUNT_PENDING.pop(uid, None)
    if st:
        try:
            count = int(text)
            if count < 1:
                count = 1
            if count > 100:
                count = 100
        except ValueError:
            await message.reply("Please enter a number (1-100).")
            _COUNT_PENDING[uid] = st
            return
        await _fetch_and_send(message.chat.id, uid, st["name"], st["source"], st["tag"], count, bot)
        return

    if _BOT_USERNAME and f"@{_BOT_USERNAME}" in text:
        await message.answer("Choose a branch:", reply_markup=branch_keyboard())

# ── main ──────────────────────────────────────────────────
def _serve_health(port):
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

async def main():
    global _BOT_USERNAME
    if not TOKEN:
        raise SystemExit("BOT_TOKEN not set")
    dp = Dispatcher()
    proxy = os.getenv("https_proxy") or os.getenv("http_proxy")
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(token=TOKEN, session=session)
    try:
        _BOT_USERNAME = (await bot.me()).username or ""
        await bot.set_my_commands([BotCommand(c, d) for c, d in (
            ("start", "Welcome"),
            ("menu", "Browse members → sources → tags"),
            ("members", "List members"),
            ("tags", "List a member's tags"),
            ("sources", "List sources"),
            ("mode", "Photo (HD) vs file (full res)"),
        )])
    except Exception:
        pass

    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_menu, Command("menu"))
    dp.message.register(cmd_members, Command("members"))
    dp.message.register(cmd_tags, Command("tags"))
    dp.message.register(cmd_sources, Command("sources"))
    dp.message.register(cmd_mode, Command("mode"))
    dp.callback_query.register(on_menu_click)
    dp.message.register(on_message, F.text)

    _serve_health(PORT)
    print(f"Bot starting (health on :{PORT})...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())