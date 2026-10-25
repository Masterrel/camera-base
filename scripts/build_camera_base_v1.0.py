#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Збірка бази камер для застосунку Camera Alert (запускається GitHub Actions раз на добу).

Що робить:
  1. Тягне камери контролю швидкості по Україні з OpenStreetMap (Overpass API),
     по черзі пробуючи кілька дзеркал.
  2. Перетворює у компактний GeoJSON (формат, який розуміє застосунок).
  3. Перевіряє, що дані не "биті": камер не менше MIN_ABS_COUNT і не менше
     MIN_RATIO від попередньої бази. Якщо перевірка не пройдена — стара база
     НЕ перезаписується, скрипт завершується з помилкою.
  4. Пише в папку --out (за замовчуванням docs):
       cameras.json — сама база (переписується тільки якщо дані змінились);
       meta.json    — маленький файл: кількість, sha256, дати. Застосунок
                      спершу качає тільки його.

Тільки стандартна бібліотека Python 3.8+ (нічого встановлювати не треба).

Запуск:
    python build_camera_base_v1.0.py
    python build_camera_base_v1.0.py --out docs
    python build_camera_base_v1.0.py --from-file overpass.json   (тест без інтернету)

Дані: © OpenStreetMap contributors, ліцензія ODbL.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

VERSION = "1.0"

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

# Захист від битих відповідей Overpass.
MIN_ABS_COUNT = 50      # менше — точно помилка
MIN_RATIO = 0.80        # нова база не може бути меншою за 80% попередньої

CAMERAS_FILE = "cameras.json"
META_FILE = "meta.json"
FORMAT_VERSION = 1
SOURCE_TEXT = "© OpenStreetMap contributors, ODbL"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_overpass():
    """Запит до Overpass з перебором дзеркал; кожне дзеркало — до 2 спроб."""
    repo = os.environ.get("GITHUB_REPOSITORY", "local")
    ua = f"CameraAlert-base-builder/{VERSION} (+https://github.com/{repo})"
    body = urllib.parse.urlencode({"data": OVERPASS_QUERY}).encode("utf-8")
    last_error = None
    for url in OVERPASS_ENDPOINTS:
        for attempt in (1, 2):
            try:
                log(f"Запит до {url} (спроба {attempt}) ...")
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={
                        "User-Agent": ua,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=240) as resp:
                    raw = resp.read()
                data = json.loads(raw.decode("utf-8"))
                if "elements" not in data:
                    raise ValueError("у відповіді немає 'elements'")
                return data
            except Exception as exc:  # мережа, HTTP, JSON
                last_error = exc
                log(f"  не вдалося: {exc}")
                time.sleep(20 if attempt == 1 else 5)
    raise RuntimeError(f"Усі дзеркала Overpass недоступні. Остання помилка: {last_error}")


def element_coords(el):
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    center = el.get("center") or {}
    return center.get("lat"), center.get("lon")


def build_geojson(elements):
    """Компактний GeoJSON, відсортований за id — щоб однакові дані давали однаковий sha256."""
    seen = set()
    features = []
    for el in elements:
        lat, lon = element_coords(el)
        if lat is None or lon is None:
            continue
        osm_id = f"{el.get('type', '?')}/{el.get('id', '')}"
        if osm_id in seen:
            continue
        seen.add(osm_id)
        tags = el.get("tags") or {}
        props = {"osm_id": osm_id}
        if tags.get("maxspeed"):
            props["maxspeed"] = str(tags["maxspeed"])
        if tags.get("name"):
            props["name"] = str(tags["name"])
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]},
            "properties": props,
        })
    features.sort(key=lambda f: f["properties"]["osm_id"])
    return {"type": "FeatureCollection", "source": SOURCE_TEXT, "features": features}


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
    ap = argparse.ArgumentParser(description="Збірка бази камер для Camera Alert")
    ap.add_argument("--out", default="docs", help="папка для cameras.json і meta.json")
    ap.add_argument("--from-file", default=None,
                    help="взяти відповідь Overpass з файлу замість інтернету (для тесту)")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    cameras_path = os.path.join(out_dir, CAMERAS_FILE)
    meta_path = os.path.join(out_dir, META_FILE)

    old_meta = read_json(meta_path) or {}
    old_count = int(old_meta.get("count") or 0)
    old_sha = old_meta.get("sha256") or ""

    # 1. Дані
    if args.from_file:
        log(f"Читаю відповідь Overpass з файлу: {args.from_file}")
        with open(args.from_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = fetch_overpass()

    geo = build_geojson(data.get("elements", []))
    count = len(geo["features"])
    log(f"Отримано камер: {count} (попередня база: {old_count})")

    # 2. Перевірка на биті дані
    if count < MIN_ABS_COUNT:
        log(f"ПОМИЛКА: камер менше {MIN_ABS_COUNT} — схоже на збій Overpass. База не змінена.")
        return 2
    if old_count and count < old_count * MIN_RATIO:
        log(f"ПОМИЛКА: камер {count}, це менше {int(MIN_RATIO * 100)}% від попередніх {old_count}. "
            f"База не змінена.")
        return 2

    # 3. Запис
    payload = to_bytes(geo)
    sha = hashlib.sha256(payload).hexdigest()
    ts = now_iso()

    if sha == old_sha and os.path.exists(cameras_path):
        log("Дані не змінились — cameras.json не переписую, оновлюю тільки дату перевірки.")
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
        "data_updated_at": data_updated_at,  # коли востаннє змінився вміст бази
        "checked_at": ts,                    # коли востаннє успішно звірено з OSM
        "source": SOURCE_TEXT,
        "builder": f"build_camera_base v{VERSION}",
    }
    write_atomic(meta_path, to_bytes(meta))
    log(f"meta.json записано: {count} камер, sha256 {sha[:12]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# build_camera_base v1.0
