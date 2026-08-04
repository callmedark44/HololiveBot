#!/usr/bin/env python3
"""Fetch each member's complete tag set from the booru APIs fresh, since the local
database/*.json dumps can be stale (missing or obsolete costume numbers).

Strategy: for each member/source, read candidate tags from the DB dump for that
source (filtered to tags containing the member's snake name), then verify each
candidate's existence directly against the live API. This catches stale/missing
costume numbers without guessing tag formats or crawling thousands of pages.

Resumable — writes booru_tags.json after each member. Run on a machine that can
reach the sites (Cloudflare may block some sources from some IPs):
    python scripts/fetch_booru_tags.py [--refresh]
"""
import json, os, sys, time
import concurrent.futures

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import bot_data
import requests

OUT = os.path.join(HERE, "booru_tags.json")
PARENT_DB = os.path.join(os.path.dirname(HERE), "RemGodCatcher", "database")

# source -> (json file, url)
DB_FILES = {
    "yande.re":  ("yande_tag_names.json",  "https://yande.re/post.json"),
    "danbooru":  ("dan_tag_names.json",    "https://danbooru.donmai.us/posts.json"),
    "safebooru": ("safe_tag_names.json",   "https://safebooru.org/index.php"),
}

MAX_WORKERS = 21


def session():
    s = requests.Session()
    proxy = os.environ.get("http_proxy") or os.environ.get("https_proxy")
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    s.headers.update({"User-Agent": "RemGodCatcher/4.0 (by RemLover on GitHub)",
                      "Accept": "application/json"})
    return s


def load_db_tag_list(fname):
    """Load a DB dump file as a list of tag strings."""
    p = os.path.join(PARENT_DB, fname)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = list(data.keys())
    return data


def _ordinal(n):
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _verify_tag(s, source, tag):
    """Probe a single tag: True if >=1 post exists, False if none, None on Cloudflare."""
    _, url = DB_FILES[source]
    params = {"tags": tag, "limit": 1}
    if source == "safebooru":
        params.update({"page": "dapi", "s": "post", "q": "index", "json": 1, "pid": 0})
    try:
        r = s.get(url, params=params, timeout=20)
        if r.status_code in (429, 403):
            if r.status_code == 403 and not r.headers.get("content-type", "").startswith("application/json"):
                return None
            time.sleep(2)
            return _verify_tag(s, source, tag)
        if r.status_code != 200:
            return False
        body = r.text.strip()
        if not body or body in ("[]", "null"):
            return False
        posts = r.json()
        if isinstance(posts, dict):
            posts = posts.get("post", [])
        return bool(posts)
    except Exception:
        return False


def member_tags(s, member, source):
    """For one member/source: take candidate tags from the DB dump (those containing
    the member's snake), verify each via the API concurrently. Returns sorted list."""
    fname, _ = DB_FILES[source]
    db_tags = load_db_tag_list(fname)
    if db_tags is None:
        return []
    snake = " ".join(member.lower().split()).replace(" ", "_")

    # candidates from DB: anything containing the member's snake
    candidates = {t for t in db_tags if snake in t.lower()}

    # also probe costumed ordinals directly (DB might miss some; format varies)
    for n in range(1, 16):
        candidates.add(f"{snake}_({_ordinal(n)}_costume)")

    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fut_to_tag = {ex.submit(_verify_tag, s, source, t): t for t in sorted(candidates)}
        for fut in concurrent.futures.as_completed(fut_to_tag):
            tag = fut_to_tag[fut]
            try:
                if fut.result() is True:
                    found.append(tag)
            except Exception:
                pass
    return found


def main():
    s = session()
    refresh = "--refresh" in sys.argv
    results = {}
    if os.path.exists(OUT) and not refresh:
        results = json.load(open(OUT, encoding="utf-8"))
        print(f"resuming with {len(results)} members (use --refresh to redo all)", flush=True)
    sources = ["yande.re", "danbooru", "safebooru"]
    members = sorted(bot_data.MEMBERS)
    for i, name in enumerate(members, 1):
        if name in results and not refresh:
            continue
        per = {}
        for src in sources:
            per[src] = member_tags(s, name, src)
            print(f"  {src}: {len(per[src])} tags", flush=True)
        results[name] = per
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"[{i}/{len(members)}] {name} done", flush=True)
    print(f"done — {len(results)} members in {OUT}", flush=True)


if __name__ == "__main__":
    main()
