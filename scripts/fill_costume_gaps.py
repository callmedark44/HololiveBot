#!/usr/bin/env python3
"""Fill missing costume numbers by hand: derive the tag format from existing costume
tags per member/source and emit the missing ones into manual_tags.json (overlaid by
gen_bot_data.py). Covers the stale DB-dump gaps until fetch_booru_tags.py is run.
"""
import json, os, re, sys

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
import bot_data

OUT = os.path.join(BOT, "manual_tags.json")

ORD = lambda n: "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")

SNAKE_SPECIAL = {"La+ Darknesss": "laplus_darknesss"}
def snake(name):
    if name in SNAKE_SPECIAL:
        return SNAKE_SPECIAL[name]
    return name.lower().replace(" ", "_")

# stem + formatter per source for a member (derive from a costume tag, else the name)
def stem_fmt(member, tl, src):
    for t in tl:  # prefer an existing costume tag (captures disambiguation quirks)
        m = re.search(r"^(.*) \((\d+)(?:st|nd|rd|th) Costume\)$", t)
        if m:
            s = m.group(1)
            return s, lambda n: f"{s} ({n}{ORD(n)} Costume)"
        m = re.search(r"^(.*)_\(\d+(?:st|nd|rd|th)_costume\)", t)
        if m:
            s = m.group(1)
            return s, lambda n: f"{s}_({n}{ORD(n)}_costume)"
    if src == "zerochan":
        s = member
        return s, lambda n: f"{s} ({n}{ORD(n)} Costume)"
    s = snake(member)
    return s, lambda n: f"{s}_({n}{ORD(n)}_costume)"

def present_nums(tl):
    out = set()
    for t in tl:
        m = re.search(r"\((\d+)(?:st|nd|rd|th) Costume\)", t) or re.search(r"_\((\d+)(?:st|nd|rd|th)_costume\)", t)
        if m:
            out.add(int(m.group(1)))
    return out

manual = {}
if os.path.exists(OUT):
    manual = json.load(open(OUT, encoding="utf-8"))

# global max costume per member across ALL sources (est. of the member's real costume count)
global_max = {}
for name, srcs in bot_data.TAGS.items():
    alln = set()
    for tl in srcs.values():
        alln |= present_nums(tl)
    if alln:
        global_max[name] = max(alln)

for name, srcs in bot_data.TAGS.items():
    for src, tl in srcs.items():
        nums = present_nums(tl)
        if not nums and name not in global_max:
            continue
        # target top = own max, extended by the member's global costume count (stale-dump tail)
        target = max(nums, default=0) or global_max.get(name, 0)
        gaps = [n for n in range(1, target + 1) if n not in nums]
        if not gaps:
            continue
        stem, fmt = stem_fmt(name, tl, src)
        added = [fmt(n) for n in gaps]
        existing = set(manual.get(name, {}).get(src, []))
        manual.setdefault(name, {}).setdefault(src, []).extend(t for t in added if t not in existing)

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(manual, f, indent=2, ensure_ascii=False)

total = sum(len(v) for m in manual.values() for v in m.values())
print(f"wrote {total} manual tags across {len(manual)} members to {OUT}")
