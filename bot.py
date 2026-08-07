#!/usr/bin/env python3
"""Telegram bot: pick a hololive member -> source -> tag, fetch via existing workers, send, delete."""
import asyncio, html, importlib, json, os, re, threading
from dotenv import load_dotenv; load_dotenv()

import shared
from bot_data import MEMBERS, TAGS

from aiogram import Dispatcher, Bot, types, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import BotCommand, FSInputFile, InputMediaDocument, InputMediaPhoto

TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "8080"))
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(_BOT_DIR, "bot_users.json")
os.makedirs(os.path.join(_BOT_DIR, "database"), exist_ok=True)

DEFAULT_AMOUNT = 5
_APP_LOOP = None
_LOOP_LOCK = threading.Lock()

LANG_EMOJI = {"jp": "🇯🇵", "id": "🇮🇩", "en": "🇺🇸"}
BRANCH_LABEL = {"hololive": "hololive", "holostars": "HOLOSTARS"}
PAGE_SIZE = 10

# (key, label, report_key)
SOURCES = [
    ("yande", "Yande.re", "yande.re"),
    ("dan", "Danbooru", "danbooru"),
    ("safe", "Safebooru", "safebooru"),
    ("zero", "Zerochan", "zerochan"),
    ("nekosia", "Nekosia", "nekosia"),
    ("eshuushuu", "E-shuushuu", "eshuushuu"),
]
SOURCE_BY_REPORT = {r: k for k, _l, r in SOURCES}

FETCHERS = {
    "yande": ("workers.yande", "worker_yande", lambda t, nc, n: (t, n, "rating:s", nc)),
    "dan": ("workers.danbooru", "worker_danbooru", lambda t, nc, n: (t, n, "rating:g", [], nc)),
    "safe": ("workers.safebooru", "worker_safebooru", lambda t, nc, n: (t, n, [], nc)),
    "zero": ("workers.zerochan", "worker_zerochan", lambda t, nc, n: (t, n, nc)),
    "nekosia": ("workers.nekosia", "worker_nekosia", lambda t, nc, n: (t, n, nc)),
    "eshuushuu": ("workers.eshuushuu", "worker_eshuushuu", lambda t, nc, n: (t, n, [], "", nc)),
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
    rows.append([_btn("🎲 Random art", "rnd")])
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
    nav.append(_btn("Back", f"b:all"))
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
    mode_label = "File: ON" if as_doc else "File: OFF"
    rows.append([_btn(f"📄 {mode_label}", f"mode:{_store({'name': name, 'source': skey, 'tag': tag, 'back': 'count'})}")])
    group_label = "Album: ON" if user_pref(uid, "group", True) else "Album: OFF"
    rows.append([_btn(f"🖼 {group_label}", f"grp:{_store({'name': name, 'source': skey, 'tag': tag})}")])
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

SRC_LOOKUP = {k: k for k, _l, _r in SOURCES} | {l.lower(): k for k, l, _r in SOURCES}

async def cmd_fetch(message: types.Message, bot: Bot):
    """One-shot fetch: /fetch <member> <source> [count] [tag] [doc|photo]"""
    args = (message.text or "").split()[1:]
    si = next((i for i, a in enumerate(args) if a.lower() in SRC_LOOKUP), None)
    if si is None or si == 0:
        await message.answer("Usage: /fetch <member> <source> [count] [tag] [photo|doc]\n"
                             "e.g. /fetch Houshou Marine danbooru 5")
        return
    name_q = " ".join(args[:si]).strip()
    skey = SRC_LOOKUP[args[si].lower()]
    rest = args[si + 1:]

    label = next(l for k, l, _r in SOURCES if k == skey)

    name = next((n for n in MEMBERS if name_q.lower() in n.lower()), None)
    if not name:
        await message.answer(f"No member found for '{name_q}'. Try /members.")
        return
    uid = str(message.from_user.id)
    tl = member_tags(name, skey)
    if not tl:
        await message.answer(f"No tags for {name} on {label} yet.")
        return

    count = None
    tag = None
    send_as_doc = False

    # Parse arguments
    if rest:
        # Check if first arg is a number (count)
        if rest[0].split('-')[0].isdigit():
            count = int(rest[0])
            rest = rest[1:]

        # Check if last arg is doc/photo
        if rest and rest[-1].lower() in ("doc", "document", "file"):
            send_as_doc = True
            rest = rest[:-1]

        # Remaining is the tag
        if rest:
            tag = " ".join(rest).strip()

    # If no tag, use first available
    if tag is None:
        tag = tl[0]
    elif tag.lower() not in (t.lower() for t in tl):
        await message.answer(f"No tag '{tag}' for {name} on {label}.\n"
                             f"Known: {', '.join(tl[:8])}{'…' if len(tl) > 8 else ''}")
        return

    # If count not provided, prompt user
    if count is None:
        _COUNT_PENDING[uid] = {"name": name, "source": skey, "tag": tag, "send_as_doc": send_as_doc}
        await message.answer(f"{name} • {tag} — how many? (1-100)")
        return

    count = max(1, min(count, 100))
    await message.answer(f"Fetching {count} for {name} from {label} ({tag})… {'as files' if send_as_doc else 'as HD photos'}")
    asyncio.create_task(_fetch_and_send(message.chat.id, uid, name, skey, tag, count, bot, force_doc=send_as_doc))

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
    elif args and args[0].lower() in ("group", "album"):
        set_user_pref(uid, "group", True)
        await message.answer("Send mode: **grouped** — images sent as one album.")
    elif args and args[0].lower() in ("single", "one", "separate"):
        set_user_pref(uid, "group", False)
        await message.answer("Send mode: **single** — each image sent separately.")
    else:
        cur = "document (full res)" if user_pref(uid, "as_doc", False) else "photo (HD)"
        grp = "grouped (album)" if user_pref(uid, "group", True) else "single (one by one)"
        await message.answer(
            f"Current send mode: **{cur}**, **{grp}**\n"
            "Use /mode doc|photo for file vs inline, /mode group|single for album vs separate.")

async def cmd_random(message: types.Message, bot: Bot, **kw):
    status_msg = await message.answer("🎲 Sending random art…")
    await _send_random_art(bot, message.chat.id, uid=str(message.from_user.id), status_msg=status_msg)

# ── fetch + send ──────────────────────────────────────────
_FETCH_LOCK = threading.Lock()
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".webm", ".gif", ".bmp", ".tiff", ".tif"}

def _run_worker(skey, tag, net_config, count):
    mod_name, fn_name, extractor = FETCHERS[skey]
    mod = importlib.import_module(mod_name)
    fn = getattr(mod, fn_name)
    args = extractor(tag, net_config, count)
    fn(*args)

def _caption(name, label, tag, url=None):
    c = f"{html.escape(name)} • {html.escape(label)} • {html.escape(tag)}"
    if url:
        c += f"\n<a href=\"{html.escape(url, quote=True)}\">Link</a>"
    return c

def _is_image(fp):
    return os.path.splitext(fp)[1].lower() in IMAGE_EXT

def _use_doc(fp, as_doc):
    fsize = os.path.getsize(fp)
    return (as_doc
            or fsize > 10 * 1024 * 1024
            or os.path.splitext(fp)[1].lower() not in {".jpg", ".jpeg", ".png", ".webp"})

async def _send_file(bot, chat_id, name, label, tag, fp, as_doc, url=None):
    """Send a single downloaded file. Sequential via the sender coroutine."""
    for attempt in range(3):
        try:
            infile = FSInputFile(fp)
            caption = _caption(name, label, tag, url)
            if _use_doc(fp, as_doc):
                await bot.send_document(chat_id, infile, caption=caption, parse_mode="HTML")
            else:
                try:
                    await bot.send_photo(chat_id, infile, caption=caption, parse_mode="HTML")
                except Exception as e:
                    if "PHOTO_INVALID_DIMENSIONS" in str(e):
                        # Fall back to document for images with invalid dimensions
                        await bot.send_document(chat_id, infile, caption=caption, parse_mode="HTML")
                    else:
                        raise
            # File sent successfully — delete from disk to save storage
            try:
                os.remove(fp)
                # Clean up empty parent dirs left behind
                parent = os.path.dirname(fp)
                if parent and os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
            except OSError:
                pass
            return

        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(2)
            else:
                await bot.send_message(
                    chat_id,
                    f"Send failed for {os.path.basename(fp)}:\n{e}",
                )

async def _send_group(bot, chat_id, name, label, tag, items, as_doc):
    """Send a batch as media groups (albums). Telegram won't mix photos & documents,
    so split by type. Falls back to one-by-one per item on failure."""
    for use_doc in (False, True):
        batch = [it for it in items if _use_doc(it[0], as_doc) == use_doc]
        if not batch:
            continue
        caption = f"{html.escape(name)} • {html.escape(label)} • {html.escape(tag)} ({len(batch)})"
        for _fp, _fn, u in batch:
            link = f"\n<a href=\"{html.escape(u, quote=True)}\">Link</a>"
            if len(caption) + len(link) > 1024:  # ponytail: album caption limit; overflow links dropped
                break
            caption += link
        media = []
        for i, (fp, _fn, url) in enumerate(batch):
            cls = InputMediaDocument if use_doc else InputMediaPhoto
            kwargs = {"media": FSInputFile(fp)}
            if i == 0:
                kwargs["caption"] = caption
                kwargs["parse_mode"] = "HTML"
            media.append(cls(**kwargs))
        try:
            await bot.send_media_group(chat_id, media=media)
        except Exception:
            for fp, _fn, url in batch:
                await _send_file(bot, chat_id, name, label, tag, fp, as_doc, url)
    for fp, _fn, _url in items:
        try:
            os.remove(fp)
        except OSError:
            pass

async def _fetch_and_send(chat_id, uid, name, skey, tag, count, bot: Bot, force_doc=None, status_msg=None, quiet=False):
    label = next(l for k, l, _r in SOURCES if k == skey)
    as_doc = force_doc if force_doc is not None else user_pref(uid, "as_doc", False)
    try:
        with _FETCH_LOCK:
            proxy = os.getenv("https_proxy") or os.getenv("http_proxy")
            net_config = {"use_proxy": bool(proxy), "proxy_url": proxy or "",
                          "verify_tls": False, "anti_ban_pause": 1.0,
                          "api_timeout": 10, "retry_wait": 3, "download_retries": 5}

            send_queue = asyncio.Queue()
            use_group = user_pref(uid, "group", True)
            sent = 0

            async def sender():
                nonlocal sent
                pending = []
                while True:
                    item = await send_queue.get()
                    if item is None:
                        send_queue.task_done()
                        break
                    filepath = item[0]
                    if not _is_image(filepath):
                        send_queue.task_done()
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                        continue
                    pending.append(item)
                    send_queue.task_done()
                    if len(pending) >= 10:
                        if use_group:
                            await _send_group(bot, chat_id, name, label, tag, pending, as_doc)
                        else:
                            for fp, _fn, url in pending:
                                await _send_file(bot, chat_id, name, label, tag, fp, as_doc, url)
                        sent += len(pending)
                        pending = []
                if pending:
                    if use_group:
                        await _send_group(bot, chat_id, name, label, tag, pending, as_doc)
                    else:
                        for fp, _fn, url in pending:
                            await _send_file(bot, chat_id, name, label, tag, fp, as_doc, url)
                    sent += len(pending)

            sender_task = asyncio.create_task(sender())

            def on_download(filepath, filename, url):
                _APP_LOOP.call_soon_threadsafe(
                    send_queue.put_nowait, (filepath, filename, url)
                )

            def on_site_down(site_folder, status_code):
                code = f" (HTTP {status_code})" if status_code else ""
                msg = f"⚠️ {site_folder} is temporarily unreachable{code}. Try again later."
                if _APP_LOOP:
                    asyncio.run_coroutine_threadsafe(
                        bot.send_message(chat_id, msg), _APP_LOOP
                    )

            net_config["download_callback"] = on_download
            net_config["on_site_down"] = on_site_down
            await asyncio.to_thread(_run_worker, skey, tag, net_config, count)
            await send_queue.join()
            _APP_LOOP.call_soon_threadsafe(send_queue.put_nowait, None)
            await sender_task
            if sent == 0 and not quiet:
                await bot.send_message(
                    chat_id,
                    f"Couldn't find any new images for {tag}",
                )
            if status_msg is not None:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
    except Exception as e:
        await bot.send_message(chat_id, f"Error fetching {name} from {label}: {e}")

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

    elif kind == "grp":
        st = _get(parts[1])
        name, skey, tag = st["name"], st["source"], st["tag"]
        set_user_pref(uid, "group", not user_pref(uid, "group", True))
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
        name, skey, tag, count = st["name"], st["source"], st["tag"], st["count"]
        _COUNT_PENDING.pop(uid, None)
        status_msg = await call.message.edit_text(f"Fetching {count} for {name} from {skey}… ({tag})")
        asyncio.create_task(_fetch_and_send(call.message.chat.id, uid, name, skey, tag, count, bot, status_msg=status_msg))

    elif kind == "rnd":
        status_msg = await call.message.edit_text("🎲 Sending random art…")
        await _send_random_art(bot, call.message.chat.id, uid=uid, status_msg=status_msg)

# ── message handler ──────────────────────────────────────
_BOT_USERNAME = ""

async def _groups_only(handler, event, data):
    """Drop everything outside group/supergroup chats. Also learns the group ids
    the bot is in, since the Bot API offers no way to enumerate them."""
    chat = getattr(event, "chat", None)
    if chat is None:
        msg = getattr(event, "message", None)
        chat = getattr(msg, "chat", None)
    if not chat or chat.type not in ("group", "supergroup"):
        return
    _KNOWN_CHATS.add(str(chat.id))
    return await handler(event, data)

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

# ── random art feature ────────────────────────────────────
# Track chats we've seen so scheduler only posts where the bot is present.
_KNOWN_CHATS: set[str] = set()

async def on_chat_member(update: types.ChatMemberUpdated, bot: Bot):
    """When a new member joins a chat, send them a random art."""

    if update.new_chat_member.status in ("member", "creator", "administrator"):
        _KNOWN_CHATS.add(str(update.chat.id))
        await _send_random_art(bot, update.chat.id, uid=None)
        await bot.send_message(update.chat.id, "🎉 Welcome! Here's some art.")


async def _pick_random_art() -> tuple | None:
    """Pick a random member+source+tag combo that has downloadable art."""
    import random as _random

    candidates = []
    for name, tl in TAGS.items():
        for report_key, tags in tl.items():
            if not tags:
                continue
            candidates.append((name, SOURCE_BY_REPORT.get(report_key), tags[0]))
    if not candidates:
        return None
    return _random.choice(candidates)


async def _send_random_art(bot: Bot, chat_id, uid=None, status_msg=None):
    """Fetch + send one random art to chat."""
    picked = await _pick_random_art()
    if not picked:
        return
    name, skey, tag = picked
    as_doc = user_pref(uid or "0", "as_doc", False) if uid else False
    await _fetch_and_send(chat_id, uid or "0", name, skey, tag, 1, bot,
                          force_doc=as_doc, quiet=True, status_msg=status_msg)


async def random_art_scheduler(bot: Bot):
    """Every 4 hours, post one random art to every known chat."""
    while True:
        await asyncio.sleep(4 * 60 * 60)
        if not _KNOWN_CHATS:
            continue
        for cid in list(_KNOWN_CHATS):
            try:
                await _send_random_art(bot, int(cid))
                await asyncio.sleep(2)
            except Exception:
                pass

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
    global _APP_LOOP, _BOT_USERNAME
    _APP_LOOP = asyncio.get_running_loop()
    # Start health server FIRST so Railway's health check passes even if
    # the Telegram API handshake below hangs on a slow proxy.
    try:
        _serve_health(PORT)
    except Exception as e:
        print(f"Health server failed to start: {e}")
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
            ("random", "Send a random art"),
        )])
    except Exception:
        pass

    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_menu, Command("menu"))
    dp.message.register(cmd_members, Command("members"))
    dp.message.register(cmd_tags, Command("tags"))
    dp.message.register(cmd_sources, Command("sources"))
    dp.message.register(cmd_fetch, Command("fetch"))
    dp.message.register(cmd_mode, Command("mode"))
    dp.message.register(cmd_random, Command("random"))
    dp.message.outer_middleware(_groups_only)
    dp.callback_query.outer_middleware(_groups_only)
    dp.callback_query.register(on_menu_click)
    dp.message.register(on_message, F.text & ~F.text.startswith("/"))
    dp.chat_member.register(on_chat_member)
    dp.my_chat_member.register(on_chat_member)

    # Background task: random art every 4 hours
    asyncio.create_task(random_art_scheduler(bot))

    print(f"Bot starting (health on :{PORT})...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())