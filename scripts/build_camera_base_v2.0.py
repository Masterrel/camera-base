#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Збірка бази камер для застосунку «Камери швидкості» (запускається GitHub Actions раз на добу).
build_camera_base v2.0

Що змінилось у v2.0:
  Основне джерело — офіційна карта МВС «Адреси камер фотовідеофіксації та дозволена
  швидкість руху» (Google My Maps, яку МВС вбудовує на свою сторінку). Там усі робочі
  камери з точними GPS-координатами. OpenStreetMap тепер використовується тільки для
  обмежень швидкості (maxspeed): для кожної камери МВС береться maxspeed найближчої
  точки OSM у радіусі SPEED_MATCH_M.

Як отримуються дані МВС (по черзі, поки щось не спрацює):
  1. KML карти:   https://www.google.com/maps/d/kml?mid=<MID>&forcekml=1
  2. сторінка embed карти (розбір _pageData) — запасний варіант;
  3. якщо обидва не відповіли — пробує знайти новий mid на сторінці МВС
     (на випадок, якщо МВС замінило карту) і повторює 1–2.

Захист від битих даних: камер МВС має бути не менше MIN_ABS_COUNT і не менше
MIN_RATIO від попередньої бази МВС. Якщо не так — стара база НЕ перезаписується,
скрипт завершується з кодом 2. Якщо OSM недоступний — maxspeed береться з
попередньої бази, а збірка продовжується.

Формат cameras.json не змінився (GeoJSON, ті самі поля), застосунок міняти не треба.
Поле osm_id лишено як ідентифікатор для сумісності; для камер МВС воно має вигляд
"mvs/<серійний номер>".

Тільки стандартна бібліотека Python 3.8+.

Запуск:
    python build_camera_base_v2.0.py --out docs
Тест без інтернету:
    python build_camera_base_v2.0.py --out docs --mvs-file "збережена сторінка або kml" \
        --osm-file overpass.json
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

VERSION = "2.0"

MVS_MID = "14SmaBRAck6Ei7MKT_fDhI5ZJZNp_-NI"
MVS_PAGE = ("https://mvs.gov.ua/katalog-poslug/servisi-ta-poslugi-organiv-sistemi-vnutrisnix-sprav/"
            "avtofotovideofiksaciia-porusen-pdr/adresi-kamer-fotovideofiksaciyi-ta-dozvolena-svidkist-ruxu")
KML_URL = "https://www.google.com/maps/d/kml?mid={mid}&forcekml=1"
EMBED_URL = "https://www.google.com/maps/d/embed?mid={mid}"

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
OVERPASS_QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="UA"][admin_level=2]->.ua;
(
  node["highway"="speed_camera"](area.ua);
  node["enforcement"="maxspeed"](area.ua);
  way["enforcement"="maxspeed"](area.ua);
);
out center tags;
"""

MIN_ABS_COUNT = 200     # у МВС зараз ~426; менше 200 — точно збій
MIN_RATIO = 0.80        # не менше 80% від попередньої бази МВС
SPEED_MATCH_M = 150     # радіус пошуку maxspeed в OSM, метри

CAMERAS_FILE = "cameras.json"
META_FILE = "meta.json"
FORMAT_VERSION = 1
SOURCE_KIND = "mvs"
SOURCE_TEXT = ("Камери: МВС України (адреси камер фотовідеофіксації); "
               "обмеження швидкості: © OpenStreetMap contributors, ODbL")

F_REGION = "Область/місто"
F_DEVICE = "Назва та серійний № ПК"
F_PLACE = "Місце розташування приладу контролю"
F_DIR = "Напрямок спрямування приладу"
F_GPS = "GPS координати"

UA = f"CameraAlert-base-builder/{VERSION} (+https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'local')})"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "uk"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- МВС: розбір

def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_kml(raw):
    """KML з Google My Maps -> список словників {lat, lon, поля...}."""
    root = ET.fromstring(raw)
    ns = {"k": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    p = "k:" if ns else ""
    out = []
    for pm in root.iter(("{%s}" % ns["k"] if ns else "") + "Placemark"):
        coord = pm.find(f".//{p}Point/{p}coordinates", ns)
        if coord is None or not (coord.text or "").strip():
            continue
        lon, lat = [float(v) for v in coord.text.strip().split(",")[:2]]
        rec = {"lat": lat, "lon": lon}
        for d in pm.findall(f".//{p}ExtendedData/{p}Data", ns):
            v = d.find(f"{p}value", ns)
            rec[d.get("name")] = v.text if v is not None else None
        nm = pm.find(f"{p}name", ns)
        if nm is not None and nm.text:
            rec.setdefault("_name", nm.text)
        out.append(rec)
    return out


def parse_embed(text):
    """Сторінка embed / збережена сторінка МВС -> список словників (з _pageData)."""
    m = re.search(r'_pageData = "(.*?)";', text, re.S)
    if not m:
        raise ValueError("немає _pageData")
    data = json.loads(json.loads('"' + m.group(1) + '"'))
    out = []

    def walk(x):
        if isinstance(x, list):
            if (len(x) >= 6 and isinstance(x[0], str) and re.fullmatch(r"[0-9A-F]{16}", x[0])
                    and isinstance(x[1], list) and isinstance(x[5], list)):
                lat, lon = x[1][0][0]
                rec = {"lat": float(lat), "lon": float(lon)}
                blocks = [x[5][0]] if x[5] else []
                if len(x[5]) > 3 and x[5][3]:
                    blocks += x[5][3]
                for fld in blocks:
                    if fld and isinstance(fld, list) and isinstance(fld[0], str):
                        v = fld[1]
                        rec[fld[0]] = v[0] if v and v[0] is not None else (v[2] if v and len(v) > 2 else None)
                out.append(rec)
                return
            for y in x:
                walk(y)

    walk(data)
    return out


def parse_any(raw):
    head = raw[:2000].lstrip()
    if head.startswith(b"<?xml") or b"<kml" in head:
        return parse_kml(raw)
    return parse_embed(raw.decode("utf-8", "replace"))


def read_mvs_file(path):
    """Локальний файл: kml, html (з папкою *_files поруч) або zip збереженої сторінки."""
    import zipfile
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for i in z.infolist():
                b = z.read(i) if not i.is_dir() else b""
                if b"_pageData" in b and b"GPS" in b:
                    return parse_any(b)
        raise ValueError("у zip немає карти МВС")
    files = [path]
    side = os.path.splitext(path)[0] + "_files"
    if os.path.isdir(side):
        files += [os.path.join(dp, f) for dp, _, fs in os.walk(side) for f in fs]
    for f in files:
        with open(f, "rb") as fh:
            b = fh.read()
        if b"<kml" in b[:2000] or (b"_pageData" in b and b"GPS" in b):
            return parse_any(b)
    raise ValueError("не знайдено даних карти МВС")


def fetch_mvs_mid(mid):
    for kind, url in (("KML", KML_URL), ("embed", EMBED_URL)):
        u = url.format(mid=mid)
        for attempt in (1, 2):
            try:
                log(f"МВС: {kind} {u} (спроба {attempt})")
                recs = parse_any(http_get(u, timeout=90))
                if recs:
                    log(f"  отримано {len(recs)} камер")
                    return recs
                raise ValueError("0 камер")
            except Exception as exc:
                log(f"  не вдалося: {exc}")
                time.sleep(10)
    return None


def find_mid_on_mvs_page():
    try:
        log(f"МВС: шукаю mid карти на {MVS_PAGE}")
        html = http_get(MVS_PAGE, timeout=60).decode("utf-8", "replace")
        mids = re.findall(r"maps/d/(?:u/\d+/)?(?:embed|viewer|edit)\?mid=([A-Za-z0-9_\-]+)", html)
        return mids[0] if mids else None
    except Exception as exc:
        log(f"  сторінка МВС недоступна: {exc}")
        return None


def fetch_mvs():
    recs = fetch_mvs_mid(MVS_MID)
    if recs:
        return recs, MVS_MID
    new_mid = find_mid_on_mvs_page()
    if new_mid and new_mid != MVS_MID:
        log(f"УВАГА: на сторінці МВС інший mid карти: {new_mid} — оновіть MVS_MID у скрипті.")
        recs = fetch_mvs_mid(new_mid)
        if recs:
            return recs, new_mid
    return None, None


# ---------------------------------------------------------------- OSM: maxspeed

def fetch_overpass():
    body = urllib.parse.urlencode({"data": OVERPASS_QUERY}).encode("utf-8")
    for url in OVERPASS_ENDPOINTS:
        for attempt in (1, 2):
            try:
                log(f"OSM: {url} (спроба {attempt})")
                req = urllib.request.Request(url, data=body, method="POST", headers={
                    "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=240) as r:
                    data = json.loads(r.read().decode("utf-8"))
                if "elements" in data:
                    return data
            except Exception as exc:
                log(f"  не вдалося: {exc}")
                time.sleep(20 if attempt == 1 else 5)
    return None


def osm_speed_points(data):
    pts = []
    for el in (data or {}).get("elements", []):
        sp = (el.get("tags") or {}).get("maxspeed")
        if not sp or not str(sp).strip().isdigit():
            continue
        if el.get("type") == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is not None and lon is not None:
            pts.append((float(lat), float(lon), str(sp).strip()))
    return pts


def dist_m(a, b, c, d):
    r = math.radians
    return 6371000 * math.hypot(r(d - b) * math.cos(r((a + c) / 2)), r(c - a))


def nearest_speed(lat, lon, pts):
    best, bd = None, SPEED_MATCH_M
    for la, lo, sp in pts:
        if abs(la - lat) > 0.003 or abs(lo - lon) > 0.005:
            continue
        d = dist_m(lat, lon, la, lo)
        if d <= bd:
            best, bd = sp, d
    return best


# ---------------------------------------------------------------- збірка

def make_id(rec):
    dev = clean(rec.get(F_DEVICE))
    serial = re.sub(r"[^0-9A-Za-zА-Яа-яІіЇїЄєҐґ\-]+", "_", dev).strip("_")
    return "mvs/" + (serial or f"{rec['lat']:.5f}_{rec['lon']:.5f}")


def build_geojson(recs, speed_pts, old_speed_by_id):
    feats, seen = [], set()
    speed_from_osm = speed_from_old = 0
    for rec in recs:
        lat, lon = rec["lat"], rec["lon"]
        if not (44.0 < lat < 52.6 and 22.0 < lon < 40.5):
            continue
        fid = make_id(rec)
        base, n = fid, 2
        while fid in seen:            # однаковий серійний номер — додаємо суфікс
            fid, n = f"{base}#{n}", n + 1
        seen.add(fid)
        props = {"osm_id": fid}
        sp = nearest_speed(lat, lon, speed_pts) if speed_pts else None
        if sp:
            speed_from_osm += 1
        elif fid in old_speed_by_id:
            sp = old_speed_by_id[fid]
            speed_from_old += 1
        if sp:
            props["maxspeed"] = sp
        place = clean(rec.get(F_PLACE) or rec.get("_name"))
        if place:
            props["name"] = place
        direction = clean(rec.get(F_DIR))
        if direction:
            props["direction"] = direction
        region = clean(rec.get(F_REGION))
        if region:
            props["region"] = region
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]},
            "properties": props,
        })
    feats.sort(key=lambda f: f["properties"]["osm_id"])
    log(f"maxspeed: з OSM {speed_from_osm}, з попередньої бази {speed_from_old}, "
        f"без обмеження {len(feats) - speed_from_osm - speed_from_old}")
    return {"type": "FeatureCollection", "source": SOURCE_TEXT, "features": feats}


def to_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_atomic(path, data_bytes):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data_bytes)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Збірка бази камер (МВС + maxspeed з OSM)")
    ap.add_argument("--out", default="docs")
    ap.add_argument("--mvs-file", help="тест: KML / збережена сторінка МВС / zip замість інтернету")
    ap.add_argument("--osm-file", help="тест: відповідь Overpass (json) замість інтернету")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    cameras_path = os.path.join(out_dir, CAMERAS_FILE)
    meta_path = os.path.join(out_dir, META_FILE)

    old_meta = read_json(meta_path) or {}
    old_base = read_json(cameras_path) or {}
    old_is_mvs = old_meta.get("source_kind") == SOURCE_KIND
    old_count = int(old_meta.get("count") or 0) if old_is_mvs else 0
    old_sha = old_meta.get("sha256") or ""
    old_speed_by_id = {}
    if old_is_mvs:
        for f in old_base.get("features", []):
            pr = f.get("properties", {})
            if pr.get("maxspeed"):
                old_speed_by_id[pr.get("osm_id")] = pr["maxspeed"]

    # 1. Камери МВС
    if args.mvs_file:
        log(f"МВС з файлу: {args.mvs_file}")
        recs, mid = read_mvs_file(args.mvs_file), MVS_MID
    else:
        recs, mid = fetch_mvs()
    if not recs:
        log("ПОМИЛКА: дані МВС недоступні. База не змінена.")
        return 2

    # 2. Обмеження швидкості з OSM (не обов'язково)
    if args.osm_file:
        with open(args.osm_file, "r", encoding="utf-8") as f:
            osm = json.load(f)
    else:
        osm = fetch_overpass()
    if osm is None:
        log("OSM недоступний — maxspeed беру з попередньої бази.")
    speed_pts = osm_speed_points(osm)

    geo = build_geojson(recs, speed_pts, old_speed_by_id)
    count = len(geo["features"])
    log(f"Камер МВС: {count} (попередня база МВС: {old_count or '—'})")

    # 3. Перевірка
    if count < MIN_ABS_COUNT:
        log(f"ПОМИЛКА: камер менше {MIN_ABS_COUNT}. База не змінена.")
        return 2
    if old_count and count < old_count * MIN_RATIO:
        log(f"ПОМИЛКА: камер {count} — менше {int(MIN_RATIO * 100)}% від {old_count}. База не змінена.")
        return 2

    # 4. Запис
    payload = to_bytes(geo)
    sha = hashlib.sha256(payload).hexdigest()
    ts = now_iso()
    if sha == old_sha and os.path.exists(cameras_path):
        log("Дані не змінились — cameras.json не переписую.")
        data_updated_at = old_meta.get("data_updated_at") or ts
    else:
        write_atomic(cameras_path, payload)
        data_updated_at = ts
        log(f"cameras.json оновлено ({len(payload)} байт).")

    meta = {
        "format": FORMAT_VERSION,
        "count": count,
        "sha256": sha,
        "size_bytes": len(payload),
        "data_updated_at": data_updated_at,
        "checked_at": ts,
        "source": SOURCE_TEXT,
        "source_kind": SOURCE_KIND,
        "mvs_mid": mid,
        "osm_speed_ok": osm is not None,
        "builder": f"build_camera_base v{VERSION}",
    }
    write_atomic(meta_path, to_bytes(meta))
    log(f"meta.json записано: {count} камер, sha256 {sha[:12]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# build_camera_base v2.0
