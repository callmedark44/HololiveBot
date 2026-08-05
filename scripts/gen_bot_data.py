#!/usr/bin/env python3
"""Generate bot_data.py straight from the booru tag DBs in the parent RemGodCatcher
repo (authoritative), then overlay zerochan tags from zerochan_tags.json if present.

Run: python scripts/gen_bot_data.py
"""
import json, os, re, sys

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BOT, "bot_data.py")
PARENT_DB = os.path.join(os.path.dirname(BOT), "RemGodCatcher", "database")

# (lang, branch, gen, display, [snake names])
M = [
    ("jp","hololive","0th","Tokino Sora",["tokino_sora"]),("jp","hololive","0th","Roboco",["roboco"]),
    ("jp","hololive","0th","Sakura Miko",["sakura_miko"]),("jp","hololive","0th","Hoshimachi Suisei",["hoshimachi_suisei"]),
    ("jp","hololive","0th","AZKi",["azki"]),
    ("jp","hololive","1st","Shirakami Fubuki",["shirakami_fubuki"]),("jp","hololive","1st","Natsuiro Matsuri",["natsuiro_matsuri"]),
    ("jp","hololive","1st","Aki Rosenthal",["aki_rosenthal"]),("jp","hololive","1st","Akai Haato",["akai_haato"]),
    ("jp","hololive","Gamers","Ookami Mio",["ookami_mio"]),("jp","hololive","Gamers","Nekomata Okayu",["nekomata_okayu"]),
    ("jp","hololive","Gamers","Inugami Korone",["inugami_korone"]),
    ("jp","hololive","2nd","Nakiri Ayame",["nakiri_ayame"]),("jp","hololive","2nd","Yuzuki Choco",["yuzuki_choco"]),
    ("jp","hololive","2nd","Oozora Subaru",["oozora_subaru"]),
    ("jp","hololive","3rd","Usada Pekora",["usada_pekora"]),("jp","hololive","3rd","Shiranui Flare",["shiranui_flare"]),
    ("jp","hololive","3rd","Shirogane Noel",["shirogane_noel"]),("jp","hololive","3rd","Houshou Marine",["houshou_marine"]),
    ("jp","hololive","4th","Tsunomaki Watame",["tsunomaki_watame"]),("jp","hololive","4th","Tokoyami Towa",["tokoyami_towa"]),
    ("jp","hololive","4th","Himemori Luna",["himemori_luna"]),
    ("jp","hololive","5th","Yukihana Lamy",["yukihana_lamy"]),("jp","hololive","5th","Momosuzu Nene",["momosuzu_nene"]),
    ("jp","hololive","5th","Shishiro Botan",["shishiro_botan"]),("jp","hololive","5th","Omaru Polka",["omaru_polka"]),
    ("jp","hololive","6th","La+ Darknesss",["laplus_darknesss"]),("jp","hololive","6th","Takane Lui",["takane_lui"]),
    ("jp","hololive","6th","Hakui Koyori",["hakui_koyori"]),("jp","hololive","6th","Sakamata Chloe",["sakamata_chloe"]),
    ("jp","hololive","6th","Kazama Iroha",["kazama_iroha"]),
    ("jp","hololive","ReGLOSS","Otonose Kanade",["otonose_kanade"]),("jp","hololive","ReGLOSS","Ichijou Ririka",["ichijou_ririka"]),
    ("jp","hololive","ReGLOSS","Juufuutei Raden",["juufuutei_raden"]),("jp","hololive","ReGLOSS","Todoroki Hajime",["todoroki_hajime"]),
    ("jp","hololive","FLOW GLOW","Isaki Riona",["isaki_riona"]),("jp","hololive","FLOW GLOW","Koganei Niko",["koganei_niko"]),
    ("jp","hololive","FLOW GLOW","Mizumiya Su",["mizumiya_su"]),("jp","hololive","FLOW GLOW","Rindo Chihaya",["rindo_chihaya"]),
    ("jp","hololive","FLOW GLOW","Kikirara Vivi",["kikirara_vivi"]),
    ("jp","holostars","1st","Hanasaki Miyabi",["hanasaki_miyabi"]),("jp","holostars","1st","Kanade Izuru",["kanade_izuru"]),
    ("jp","holostars","1st","Arurandeisu",["arurandeisu"]),("jp","holostars","1st","Rikkaroid",["rikkaroid"]),
    ("jp","holostars","2nd","Astel Leda",["astel_leda"]),("jp","holostars","2nd","Kishido Temma",["kishido_temma"]),
    ("jp","holostars","2nd","Yukoku Roberu",["yukoku_roberu"]),
    ("jp","holostars","3rd","Kageyama Shien",["kageyama_shien"]),("jp","holostars","3rd","Aragami Oga",["aragami_oga"]),
    ("jp","holostars","UPROAR","Yatogami Fuma",["yatogami_fuma"]),("jp","holostars","UPROAR","Utsugi Uyu",["utsugi_uyu"]),
    ("jp","holostars","UPROAR","Minase Rio",["minase_rio"]),
    ("id","hololive","1st","Ayunda Risu",["ayunda_risu"]),("id","hololive","1st","Moona Hoshinova",["moona_hoshinova"]),
    ("id","hololive","1st","Airani Iofifteen",["airani_iofifteen"]),
    ("id","hololive","2nd","Kureiji Ollie",["kureiji_ollie"]),("id","hololive","2nd","Anya Melfissa",["anya_melfissa"]),
    ("id","hololive","2nd","Pavolia Reine",["pavolia_reine"]),
    ("id","hololive","3rd","Vestia Zeta",["vestia_zeta"]),("id","hololive","3rd","Kaela Kovalskia",["kaela_kovalskia"]),
    ("id","hololive","3rd","Kobo Kanaeru",["kobo_kanaeru"]),
    ("en","hololive","Myth","Mori Calliope",["mori_calliope"]),("en","hololive","Myth","Takanashi Kiara",["takanashi_kiara"]),
    ("en","hololive","Myth","Ninomae Ina'nis",["ninomae_ina'nis","ninomae_ina_nis"]),
    ("en","hololive","Myth","Watson Amelia",["watson_amelia"]),
    ("en","hololive","Promise","IRyS",["irys"]),("en","hololive","Promise","Ouro Kronii",["ouro_kronii"]),
    ("en","hololive","Promise","Hakos Baelz",["hakos_baelz"]),
    ("en","hololive","Advent","Shiori Novella",["shiori_novella"]),("en","hololive","Advent","Koseki Bijou",["koseki_bijou"]),
    ("en","hololive","Advent","Nerissa Ravencroft",["nerissa_ravencroft"]),
    ("en","hololive","Advent","Fuwawa Abyssgard",["fuwawa_abyssgard"]),("en","hololive","Advent","Mococo Abyssgard",["mococo_abyssgard"]),
    ("en","hololive","Justice","Elizabeth Rose Bloodflame",["elizabeth_rose_bloodflame"]),
    ("en","hololive","Justice","Gigi Murin",["gigi_murin"]),("en","hololive","Justice","Cecilia Immergreen",["cecilia_immergreen"]),
    ("en","hololive","Justice","Raora Panthera",["raora_panthera"]),
    ("en","holostars","Tempus","Regis Altare",["regis_altare"]),("en","holostars","Tempus","Axel Syrios",["axel_syrios"]),
    ("en","holostars","Tempus","Gavis Bettel",["gavis_bettel"]),("en","holostars","Tempus","Machina X Flayon",["machina_x_flayon"]),
    ("en","holostars","Tempus","Banzoin Hakka",["banzoin_hakka"]),("en","holostars","Tempus","Josuiji Shinri",["josuiji_shinri"]),
    ("en","holostars","Armis","Jurard T Rexford",["jurard_t_rexford"]),("en","holostars","Armis","Goldbullet",["goldbullet"]),
    ("en","holostars","Armis","Octavio",["octavio"]),("en","holostars","Armis","Crimzon Ruze",["crimzon_ruze"]),
]

multi, single = {}, {}
for i, (_l, _b, _g, _d, snakes) in enumerate(M):
    for s in snakes:
        key = re.sub(r"[\W_]+", "", s.lower())  # normalize: strip underscores/spaces
        (single if "_" not in s else multi)[key] = i

def load_tags(fname):
    p = os.path.join(PARENT_DB, fname)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        if fname == "anime_tags.json":
            return [json.loads(line)["tag"] for line in f if line.strip()]
        if fname == "eshuushuu_tags.json":
            return [t["title"] for t in json.load(f) if t.get("title")]
        return json.load(f)

def member_for(tag):
    t = tag.lower()
    core = re.sub(r"[\W_]+", "", t.split("(", 1)[0])
    if core in multi:
        return multi[core]
    if core in single:
        # single-token names are ambiguous: only accept exact or explicit hololive/holostars
        if t.split("(")[0].rstrip("_") == core or "(hololive" in t or "(holostars" in t:
            return single[core]
    # member snake can appear as a parenthesised disambiguation, e.g. "deadbeat_(mori_calliope)"
    for s, idx in multi.items():
        if f"({s})" in t or f"({s}_" in t:
            return idx
    return None

WORKERS = [("yande.re", "yande_tag_names.json"), ("danbooru", "dan_tag_names.json"),
           ("safebooru", "safe_tag_names.json"), ("nekosia", "nekosia_tag_names.json"),
           ("eshuushuu", "eshuushuu_tags.json")]

tags = {M[i][3]: {} for i in range(len(M))}   # display -> {source: [tag,...]}
for wname, fname in WORKERS:
    for tag in load_tags(fname):
        idx = member_for(tag)
        if idx is not None:
            tags.setdefault(M[idx][3], {}).setdefault(wname, set()).add(tag)

# overlay complete zerochan tags from fetch_zerochan_tags.py if present
zc_file = os.path.join(BOT, "zerochan_tags.json")
if os.path.exists(zc_file):
    for name, tl in json.load(open(zc_file, encoding="utf-8")).items():
        tags.setdefault(name, {}).setdefault("zerochan", set()).update(tl)

# overlay fresh booru tags from fetch_booru_tags.py if present (union per source)
br_file = os.path.join(BOT, "booru_tags.json")

# Load DB dump tags for validation
db_tags = {}
for fname, sname in [("yande_tag_names.json", "yande.re"), ("kona_tag_names.json", "konachan"),
                     ("dan_tag_names.json", "danbooru"), ("safe_tag_names.json", "safebooru")]:
    db_tags[sname] = set(load_tags(fname))

if os.path.exists(br_file):
    for name, srcs in json.load(open(br_file, encoding="utf-8")).items():
        for src, tl in srcs.items():
            # Only overlay tags that exist in the DB dump for that source
            if src in db_tags:
                tags.setdefault(name, {}).setdefault(src, set()).update(t for t in tl if t in db_tags[src])
            else:
                tags.setdefault(name, {}).setdefault(src, set()).update(tl)

# overlay hand-added costume tags from manual_tags.json if present (union)
man_file = os.path.join(BOT, "manual_tags.json")
if os.path.exists(man_file):
    for name, srcs in json.load(open(man_file, encoding="utf-8")).items():
        for src, tl in srcs.items():
            # Only overlay tags that exist in the DB dump for that source
            if src in db_tags:
                tags.setdefault(name, {}).setdefault(src, set()).update(t for t in tl if t in db_tags[src])
            else:
                tags.setdefault(name, {}).setdefault(src, set()).update(tl)

with open(OUT, "w", encoding="utf-8") as f:
    f.write("# Generated by scripts/gen_bot_data.py from the booru tag DBs — do not edit.\n")
    f.write("MEMBERS = {\n")
    for name in (M[i][3] for i in range(len(M))):
        f.write(f"    {name!r}: {{'lang': {M[[i for i in range(len(M)) if M[i][3]==name][0]][0]!r}, "
                f"'branch': {M[[i for i in range(len(M)) if M[i][3]==name][0]][1]!r}, "
                f"'gen': {M[[i for i in range(len(M)) if M[i][3]==name][0]][2]!r}}},\n")
    f.write("}\n\n")
    f.write("TAGS = {\n")
    for i in range(len(M)):
        name = M[i][3]
        f.write("    " + repr(name) + ": {\n")
        for src, tl in tags.get(name, {}).items():
            f.write(f"        {src!r}: {sorted(tl)!r},\n")
        f.write("    },\n")
    f.write("}\n")

print(f"members: {len(M)}")
print(f"sources: {sorted({s for t in tags.values() for s in t})}")
pairs = sum(1 for t in tags.values() if t)
print(f"members with any tags: {pairs}")
if len(M) != 87:
    sys.exit(f"ERROR: expected 87 members, got {len(M)}")
