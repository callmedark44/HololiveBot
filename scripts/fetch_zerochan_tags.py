#!/usr/bin/env python3
"""Re-fetch each member's zerochan tags with pagination + dedup + 429 retry.

Writes zerochan_tags.json in the project root (resumable). Run on a machine
that can reach zerochan.net (the sandbox can't):
    python scripts/fetch_zerochan_tags.py
"""
import json, os, time, urllib.parse, requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys_path = __import__("sys"); sys_path.path.insert(0, HERE)
import bot_data

OUT = os.path.join(HERE, "zerochan_tags.json")
MAX_PAGES = 30
PAGE_SIZE = 100
SLEEP = 1.0

def session():
    s = requests.Session()
    s.headers.update({"User-Agent": "RemGodCatcher/4.0 (by RemLover on GitHub)",
                      "X-Requested-With": "XMLHttpRequest",
                      "Accept": "application/json, text/javascript, */*; q=0.01"})
    return s

def canonical_tags(s, member):
    q = urllib.parse.quote_plus(member)
    found = set()
    empty_pages = 0
    for page in range(1, MAX_PAGES + 1):
        for attempt in range(3):
            try:
                r = s.get(f"https://www.zerochan.net/{q}",
                          params={"json": "1", "p": page, "l": PAGE_SIZE}, timeout=30)
                if r.status_code == 429:
                    time.sleep(5 + attempt * 5)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except Exception:
                if attempt == 2:
                    return sorted(found)
                time.sleep(3)
        else:
            return sorted(found)

        # if the tag page returns its own full tag list at top level, grab it
        top = data.get("tags")
        if isinstance(top, list):
            found.update(t for t in top if isinstance(t, str) and member.lower() in t.lower())

        items = data.get("items", [])
        if not items:
            break
        new = 0
        for item in items:
            for t in item.get("tags") or []:
                if isinstance(t, str) and member.lower() in t.lower() and t not in found:
                    found.add(t)
                    new += 1
        empty_pages = 0 if new else empty_pages + 1
        if empty_pages >= 3:
            break
        time.sleep(SLEEP)
    return sorted(found)

def main():
    s = session()
    refresh = "--refresh" in __import__("sys").argv
    results = {}
    if os.path.exists(OUT) and not refresh:
        results = json.load(open(OUT, encoding="utf-8"))
        print(f"resuming with {len(results)} members already done (use --refresh to re-fetch all)")
    members = sorted(bot_data.MEMBERS)
    for i, name in enumerate(members, 1):
        if name in results and not refresh:
            continue
        tags = canonical_tags(s, name)
        results[name] = tags
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"[{i}/{len(members)}] {name}: {len(tags)} tags")
    print(f"done — {len(results)} members written to {OUT}")

if __name__ == "__main__":
    main()
