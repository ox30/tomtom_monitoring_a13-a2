"""
TomTom Traffic Capture via API — GitHub Actions
================================================
Couches superposées :
  1. Carte de base       — Map Display API (tuiles raster)
  2. Traffic Flow        — Traffic API Raster Flow Tiles
  3. Traffic Incidents   — Incident Details API v5 (dessin vectoriel Pillow)

La couche incidents est dessinée en lignes pointillées fines
pour reproduire fidèlement le rendu de plan.tomtom.com :
  - Rouge = fermetures de route (Road Closed, iconCategory 8)
  - Gris  = tous les autres incidents

Horodatage : Europe/Zurich (CET/CEST)
Structure  : captures/YYYY-MM-DD/zone_name/YYYY-MM-DD-HHMM_zone_name.jpg
Rétention  : 7 jours
"""

import math
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw

# =============================================================================
# CONFIGURATION
# =============================================================================

API_KEY = os.environ.get("TOMTOM_API_KEY", "")

# Zones — collez directement l'URL de plan.tomtom.com
ZONES = {
    "zone_globale_A2_A13": "https://plan.tomtom.com/en/?p=46.68973,8.93561,8.55z",
    "zone_A13_Chur":       "https://plan.tomtom.com/en/?p=46.89942,9.32459,9.75z",
    "zone_Chur_Isla-T":    "https://plan.tomtom.com/en/?p=46.84086,9.45618,12.17z",
}

VIEWPORT_WIDTH  = 1920
VIEWPORT_HEIGHT = 1080
TILE_SIZE       = 512

OUTPUT_DIR     = Path("captures")
CACHE_DIR      = Path(".tile-cache")
TIMEZONE       = ZoneInfo("Europe/Zurich")
RETENTION_DAYS = 7

# Endpoints
BASE_URL = (
    "https://api.tomtom.com/map/1/tile/basic/main"
    "/{z}/{x}/{y}.png?tileSize={ts}&key={key}"
)
FLOW_URL = (
    "https://api.tomtom.com/traffic/map/4/tile/flow/relative"
    "/{z}/{x}/{y}.png?tileSize={ts}&thickness=2&key={key}"
)
INCIDENTS_API = "https://api.tomtom.com/traffic/services/5/incidentDetails"

# Dessin incidents
COLOR_CLOSED = (200, 30, 30, 220)       # Rouge — fermetures totales
COLOR_OTHER  = (120, 120, 120, 200)     # Gris — autres incidents
LINE_WIDTH   = 3
DASH_ON      = 8
DASH_OFF     = 6

MAX_RETRIES     = 2
REQUEST_TIMEOUT = 15
MAX_WORKERS     = 8

# =============================================================================
# COMPTEUR API
# =============================================================================

class ApiCounter:
    def __init__(self):
        self.tiles_fetched = 0
        self.tiles_cached  = 0
        self.nontile_calls = 0

    def __str__(self):
        t = self.tiles_fetched + self.tiles_cached
        return (f"Tuiles: API={self.tiles_fetched} cache={self.tiles_cached} "
                f"total={t}  |  Non-tuile: {self.nontile_calls}")

counter = ApiCounter()

# =============================================================================
# PARSEUR URL
# =============================================================================

def parse_tomtom_url(url: str) -> dict:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    p_value = params.get("p", [None])[0]
    if not p_value:
        m = re.search(r"p=([^&]+)", url)
        if not m:
            raise ValueError(f"Paramètre 'p=' introuvable: {url}")
        p_value = m.group(1)

    m = re.match(r"^(-?[\d.]+),(-?[\d.]+),([\d.]+)z?$", p_value)
    if not m:
        raise ValueError(f"Format inattendu: p='{p_value}'")

    lat       = float(m.group(1))
    lon       = float(m.group(2))
    zoom_frac = float(m.group(3))
    # floor() → utilise le niveau de zoom inférieur pour reproduire
    # la densité de routes de plan.tomtom.com (qui masque les petites
    # routes via son style vectoriel aux zooms fractionnaires)
    zoom      = int(math.floor(zoom_frac))

    return {"lat": lat, "lon": lon, "zoom": zoom, "zoom_frac": zoom_frac}


def parse_zone_config(value) -> dict:
    return parse_tomtom_url(value) if isinstance(value, str) else value

# =============================================================================
# GÉOMÉTRIE TUILES (Spherical Mercator)
# =============================================================================

def lat_lon_to_tile(lat, lon, zoom):
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def lat_lon_to_pixel(lat, lon, zoom, ts=TILE_SIZE):
    tx, ty = lat_lon_to_tile(lat, lon, zoom)
    return tx * ts, ty * ts


def tile_to_lat_lon(tx, ty, zoom):
    n = 2 ** zoom
    lon = tx / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    return lat, lon


def get_tile_grid(lat, lon, zoom, width=VIEWPORT_WIDTH, height=VIEWPORT_HEIGHT):
    cx, cy = lat_lon_to_tile(lat, lon, zoom)
    cpx, cpy = cx * TILE_SIZE, cy * TILE_SIZE
    tl_px, tl_py = cpx - width / 2, cpy - height / 2

    x0 = int(math.floor(tl_px / TILE_SIZE))
    y0 = int(math.floor(tl_py / TILE_SIZE))
    x1 = int(math.floor((tl_px + width - 1) / TILE_SIZE))
    y1 = int(math.floor((tl_py + height - 1) / TILE_SIZE))
    off_x = int(tl_px - x0 * TILE_SIZE)
    off_y = int(tl_py - y0 * TILE_SIZE)

    mx = 2 ** zoom - 1
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(mx, x1), min(mx, y1)

    return x0, y0, x1, y1, off_x, off_y


def get_viewport_bbox(lat, lon, zoom, width, height):
    cx, cy = lat_lon_to_tile(lat, lon, zoom)
    hw, hh = width / (2 * TILE_SIZE), height / (2 * TILE_SIZE)
    lat_tl, lon_tl = tile_to_lat_lon(cx - hw, cy - hh, zoom)
    lat_br, lon_br = tile_to_lat_lon(cx + hw, cy + hh, zoom)
    return (lon_tl, lat_br, lon_br, lat_tl)

# =============================================================================
# TÉLÉCHARGEMENT TUILES
# =============================================================================

session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS, max_retries=0)
session.mount("https://", _adapter)
session.headers["User-Agent"] = "TomTomCapture/2.0"


def fetch_tile(url, cache_key=None):
    if cache_key:
        cp = CACHE_DIR / cache_key
        if cp.exists():
            counter.tiles_cached += 1
            return Image.open(cp).convert("RGBA")

    for attempt in range(MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            img = Image.open(BytesIO(r.content)).convert("RGBA")
            counter.tiles_fetched += 1
            if cache_key:
                cp = CACHE_DIR / cache_key
                cp.parent.mkdir(parents=True, exist_ok=True)
                img.save(str(cp), "PNG")
            return img
        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(1.5 * (attempt + 1))
            else:
                print(f"    ✗ Tuile échouée: {e}")
                counter.tiles_fetched += 1
                return Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))

    return Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))

# =============================================================================
# INCIDENTS — API v5 + DESSIN VECTORIEL
# =============================================================================

def fetch_incidents(bbox):
    """Appelle Incident Details API v5, retourne [{coords, closed}]."""
    min_lon, min_lat, max_lon, max_lat = bbox
    fields = "{incidents{type,geometry{type,coordinates},properties{iconCategory}}}"
    url = (f"{INCIDENTS_API}?key={API_KEY}"
           f"&bbox={min_lon},{min_lat},{max_lon},{max_lat}"
           f"&fields={quote(fields, safe='{}(),')}"
           f"&language=en-GB&timeValidityFilter=present")

    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        counter.nontile_calls += 1
        data = r.json()
    except Exception as e:
        print(f"    ✗ Incident Details API: {e}")
        counter.nontile_calls += 1
        return []

    incidents = []
    for inc in data.get("incidents", []):
        geom = inc.get("geometry", {})
        props = inc.get("properties", {})
        closed = (props.get("iconCategory") == 8)
        gtype = geom.get("type", "")
        coords = geom.get("coordinates", [])

        if gtype == "LineString" and coords:
            incidents.append({"coords": coords, "closed": closed})
        elif gtype == "MultiLineString":
            for line in coords:
                if line:
                    incidents.append({"coords": line, "closed": closed})
    return incidents


def draw_dashed_line(draw, points, color, width=LINE_WIDTH,
                     dash_on=DASH_ON, dash_off=DASH_OFF):
    """Polyligne en pointillés."""
    residual = 0
    drawing = True

    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        seg_len = math.hypot(x1 - x0, y1 - y0)
        if seg_len < 1:
            continue

        ux, uy = (x1 - x0) / seg_len, (y1 - y0) / seg_len
        consumed = 0.0

        while consumed < seg_len:
            dash_len = dash_on if drawing else dash_off
            step = min(dash_len - residual, seg_len - consumed)
            if drawing and step > 0:
                sx = x0 + ux * consumed
                sy = y0 + uy * consumed
                ex = x0 + ux * (consumed + step)
                ey = y0 + uy * (consumed + step)
                draw.line([(sx, sy), (ex, ey)], fill=color, width=width)
            consumed += step
            residual += step
            if residual >= dash_len:
                residual = 0
                drawing = not drawing


def render_incidents(incidents, zoom, canvas_w, canvas_h, x0_tile, y0_tile):
    """Dessine les incidents sur un calque transparent."""
    origin_px = x0_tile * TILE_SIZE
    origin_py = y0_tile * TILE_SIZE

    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    other_lines, closed_lines = [], []

    for inc in incidents:
        pts = []
        for coord in inc["coords"]:
            px, py = lat_lon_to_pixel(coord[1], coord[0], zoom)
            pts.append((int(px - origin_px), int(py - origin_py)))
        if len(pts) < 2:
            continue
        (closed_lines if inc["closed"] else other_lines).append(pts)

    # Gris en-dessous, rouge par-dessus
    for pts in other_lines:
        draw_dashed_line(draw, pts, COLOR_OTHER)
    for pts in closed_lines:
        draw_dashed_line(draw, pts, COLOR_CLOSED)

    n = len(closed_lines) + len(other_lines)
    print(f"    → {n} incidents ({len(closed_lines)} fermetures, {len(other_lines)} autres)")
    return canvas

# =============================================================================
# ASSEMBLAGE
# =============================================================================

def build_layer(tiles, x0, y0, x1, y1):
    cols, rows = x1 - x0 + 1, y1 - y0 + 1
    canvas = Image.new("RGBA", (cols * TILE_SIZE, rows * TILE_SIZE), (0, 0, 0, 0))
    for (tx, ty), img in tiles.items():
        px = (tx - x0) * TILE_SIZE
        py = (ty - y0) * TILE_SIZE
        if img.size != (TILE_SIZE, TILE_SIZE):
            img = img.resize((TILE_SIZE, TILE_SIZE), Image.LANCZOS)
        canvas.paste(img, (px, py))
    return canvas

# =============================================================================
# CAPTURE
# =============================================================================

def capture_zone(name, config):
    now = datetime.now(TIMEZONE)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M")

    lat, lon, zoom = config["lat"], config["lon"], config["zoom"]
    zoom_frac = config.get("zoom_frac", float(zoom))

    # Avec floor(), zoom ≤ zoom_frac → scale < 1 → viewport plus petit → upscale
    # Ex: 9.75→9 : scale=0.60 → fetch 1143×643 → upscale à 1920×1080
    # Cela reproduit la densité de routes de plan.tomtom.com
    scale = 2 ** (zoom - zoom_frac)  # < 1 quand zoom < zoom_frac
    fw = max(256, round(VIEWPORT_WIDTH * scale))
    fh = max(256, round(VIEWPORT_HEIGHT * scale))
    needs_resize = abs(scale - 1.0) > 0.01

    zone_dir = OUTPUT_DIR / date_str / name
    zone_dir.mkdir(parents=True, exist_ok=True)
    filename = zone_dir / f"{date_str}-{time_str}_{name}.jpg"

    print(f"\n{'='*60}")
    print(f"[{name}] {now.strftime('%Y-%m-%d %H:%M %Z')}")
    if needs_resize:
        print(f"[{name}] zoom={zoom_frac}→{zoom}  scale={scale:.3f}"
              f"  fetch={fw}×{fh}→{VIEWPORT_WIDTH}×{VIEWPORT_HEIGHT}")
    else:
        print(f"[{name}] zoom={zoom}  center=({lat:.5f}, {lon:.5f})")

    # Grille
    x0, y0, x1, y1, off_x, off_y = get_tile_grid(lat, lon, zoom, fw, fh)
    coords = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    n = len(coords)
    print(f"[{name}] Grille: {x1-x0+1}×{y1-y0+1} = {n} tuiles/couche"
          f"  ({n*2} tuiles + 1 appel incidents)")

    # Tuiles base + flow
    base_tiles, flow_tiles = {}, {}
    futures = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for tx, ty in coords:
            burl = BASE_URL.format(z=zoom, x=tx, y=ty, ts=TILE_SIZE, key=API_KEY)
            ck = f"base/{date_str}/{zoom}/{tx}_{ty}.png"
            futures.append(("base", tx, ty, pool.submit(fetch_tile, burl, ck)))

            furl = FLOW_URL.format(z=zoom, x=tx, y=ty, ts=TILE_SIZE, key=API_KEY)
            futures.append(("flow", tx, ty, pool.submit(fetch_tile, furl, None)))

        for layer, tx, ty, fut in futures:
            img = fut.result()
            if layer == "base":
                base_tiles[(tx, ty)] = img
            else:
                flow_tiles[(tx, ty)] = img

    # Incidents
    bbox = get_viewport_bbox(lat, lon, zoom, fw, fh)
    print(f"[{name}] Incidents API (bbox={bbox[0]:.3f},{bbox[1]:.3f},{bbox[2]:.3f},{bbox[3]:.3f})...")
    incidents = fetch_incidents(bbox)

    # Assemblage
    print(f"[{name}] Assemblage...")
    base_layer = build_layer(base_tiles, x0, y0, x1, y1)
    flow_layer = build_layer(flow_tiles, x0, y0, x1, y1)
    inc_layer  = render_incidents(incidents, zoom,
                                  base_layer.size[0], base_layer.size[1], x0, y0)

    composite = Image.alpha_composite(base_layer, flow_layer)
    composite = Image.alpha_composite(composite, inc_layer)

    cropped = composite.crop((off_x, off_y, off_x + fw, off_y + fh))
    if needs_resize:
        cropped = cropped.resize((VIEWPORT_WIDTH, VIEWPORT_HEIGHT), Image.LANCZOS)

    final = cropped.convert("RGB")
    final.save(str(filename), "JPEG", quality=85, optimize=True)
    print(f"[{name}] ✓ {filename} ({filename.stat().st_size / 1024:.0f} KB)")

    return n * 2

# =============================================================================
# MAINTENANCE
# =============================================================================

def rotate_old_days():
    if not OUTPUT_DIR.exists():
        return
    cutoff = datetime.now(TIMEZONE).date() - timedelta(days=RETENTION_DAYS)
    for d in sorted(OUTPUT_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        try:
            if datetime.strptime(d.name, "%Y-%m-%d").date() < cutoff:
                shutil.rmtree(d)
                print(f"[rotation] Supprimé: {d.name}")
        except ValueError:
            continue


def clear_stale_cache():
    marker = CACHE_DIR / ".cache-date"
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    if marker.exists():
        try:
            if marker.read_text().strip() == today:
                return
        except Exception:
            pass
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    marker.write_text(today)
    print("[cache] Cache réinitialisé")

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    if not API_KEY:
        print("✗ TOMTOM_API_KEY non définie !")
        sys.exit(1)

    parsed_zones = {}
    print("Zones configurées:")
    for name, value in ZONES.items():
        try:
            cfg = parse_zone_config(value)
            parsed_zones[name] = cfg
            zf = cfg.get("zoom_frac", cfg["zoom"])
            zi = f"zoom={zf}→{cfg['zoom']}" if zf != cfg["zoom"] else f"zoom={cfg['zoom']}"
            print(f"  ✓ {name}: lat={cfg['lat']:.5f} lon={cfg['lon']:.5f} {zi}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")

    if not parsed_zones:
        print("✗ Aucune zone valide !")
        sys.exit(1)

    clear_stale_cache()

    total_tiles = 0
    errors = 0
    for zn, zc in parsed_zones.items():
        try:
            total_tiles += capture_zone(zn, zc)
        except Exception as e:
            print(f"[{zn}] ✗ Erreur: {e}")
            errors += 1

    print(f"\n{'='*60}")
    print(f"Résumé: {len(parsed_zones) - errors}/{len(parsed_zones)} zones OK")
    print(f"{counter}")
    print(f"{'='*60}")

    try:
        rotate_old_days()
    except Exception as e:
        print(f"[rotation] ✗ {e}")

    if errors == len(parsed_zones):
        sys.exit(1)
