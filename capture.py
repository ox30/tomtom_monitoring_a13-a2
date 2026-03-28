"""
TomTom Traffic Capture — Hybrid (Playwright + API)
===================================================
Approche hybride pour le meilleur rendu possible :

  1. Carte de base  → Playwright (screenshot de plan.tomtom.com SANS trafic)
                      Rendu vectoriel parfait : labels, badges A13, densité de routes.
                      Capturée 1× par démarrage de workflow (~6h).

  2. Traffic Flow   → API TomTom Raster Flow Tiles (tuiles transparentes)
                      Superposées toutes les 10 min.

  3. Incidents      → API Incident Details v5 + dessin Pillow
                      Lignes pointillées : rouge=fermetures, gris=autres.
                      Dessinées toutes les 10 min.

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
BASE_CACHE_DIR = Path(".base-cache")
TILE_CACHE_DIR = Path(".tile-cache")
TIMEZONE       = ZoneInfo("Europe/Zurich")
RETENTION_DAYS = 7

# Endpoints API
FLOW_URL = (
    "https://api.tomtom.com/traffic/map/4/tile/flow/relative"
    "/{z}/{x}/{y}.png?tileSize={ts}&thickness=2&key={key}"
)
INCIDENTS_API = "https://api.tomtom.com/traffic/services/5/incidentDetails"

# Dessin incidents
COLOR_CLOSED = (200, 30, 30, 220)
COLOR_OTHER  = (120, 120, 120, 200)
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
    zoom      = round(zoom_frac)

    return {
        "lat": lat, "lon": lon, "zoom": zoom, "zoom_frac": zoom_frac,
        "url": url,
    }


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
    return max(0, x0), max(0, y0), min(mx, x1), min(mx, y1), off_x, off_y


def get_viewport_bbox(lat, lon, zoom, width, height):
    cx, cy = lat_lon_to_tile(lat, lon, zoom)
    hw, hh = width / (2 * TILE_SIZE), height / (2 * TILE_SIZE)
    lat_tl, lon_tl = tile_to_lat_lon(cx - hw, cy - hh, zoom)
    lat_br, lon_br = tile_to_lat_lon(cx + hw, cy + hh, zoom)
    return (lon_tl, lat_br, lon_br, lat_tl)

# =============================================================================
# PLAYWRIGHT — CAPTURE CARTE DE BASE (sans trafic)
# =============================================================================

# Scripts injectés pour supprimer popups
INIT_SCRIPT = """
(() => {
    try {
        localStorage.setItem('tt.welcomeModalDismissed', 'true');
        localStorage.setItem('tt.welcomeModalSeen', 'true');
        localStorage.setItem('tt.onboarding.dismissed', 'true');
        localStorage.setItem('tt.onboarding.completed', 'true');
        localStorage.setItem('welcomeModalDismissed', 'true');
        localStorage.setItem('welcome-modal-dismissed', 'true');
        localStorage.setItem('has-seen-welcome', 'true');
        localStorage.setItem('tt.cookieConsent', 'accepted');
        localStorage.setItem('cookieConsent', 'accepted');
        localStorage.setItem('cookie-consent-accepted', 'true');
    } catch(e) {}
})();
"""

CLEANUP_CSS = """
[class*="cookie" i], [id*="cookie" i],
[class*="consent" i], [id*="consent" i],
[class*="welcome" i], [id*="welcome" i],
[class*="onboarding" i], [id*="onboarding" i],
[class*="modal" i]:not([class*="map" i]),
[id*="modal" i]:not([id*="map" i]),
[class*="backdrop" i],
[class*="toast" i], [class*="snackbar" i],
[class*="map-options" i], [class*="mapOptions" i],
[class*="sidebar" i]:not([class*="map" i]),
[class*="side-panel" i], [class*="sidePanel" i],
[class*="search" i]:not([class*="map" i]),
[class*="header" i]:not([class*="map" i]),
[class*="toolbar" i], [class*="tool-bar" i],
[class*="feedback" i], [class*="sign-in" i],
[class*="road-trip" i], [class*="roadTrip" i],
button[class*="zoom" i], [class*="controls" i]:not([class*="map" i]),
[class*="logo" i]:not([class*="map" i]),
[class*="copyright" i], [class*="attribution" i]
{
    display: none !important;
    visibility: hidden !important;
    opacity: 0 !important;
    pointer-events: none !important;
}
"""


def capture_base_maps(parsed_zones: dict) -> dict:
    """
    Capture les cartes de base de toutes les zones via Playwright.
    Pas de trafic activé — carte nue uniquement.

    Returns:
        dict[str, Path]: zone_name → chemin du PNG en cache
    """
    from playwright.sync_api import sync_playwright

    BASE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    base_maps = {}

    print(f"\n{'='*60}")
    print("CAPTURE CARTES DE BASE (Playwright)")
    print(f"{'='*60}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        context.add_init_script(INIT_SCRIPT)
        context.add_cookies([
            {"name": "cookieConsent",     "value": "accepted", "domain": ".tomtom.com",      "path": "/"},
            {"name": "tt_cookie_consent", "value": "all",      "domain": ".tomtom.com",      "path": "/"},
            {"name": "cookie-agreed",     "value": "2",        "domain": ".plan.tomtom.com", "path": "/"},
        ])

        for name, config in parsed_zones.items():
            cache_path = BASE_CACHE_DIR / f"{name}.png"
            url = config["url"]

            print(f"\n[{name}] Chargement de {url}")

            page = context.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=60_000)
                print(f"  → Page chargée (networkidle)")
                time.sleep(3)

                # Fermer les popups cookies
                for selector in [
                    "button:has-text('Accept all')",
                    "button:has-text('Accept')",
                    "[data-testid='cookie-accept']",
                    "button[class*='accept' i]",
                ]:
                    try:
                        btn = page.query_selector(selector)
                        if btn and btn.is_visible():
                            btn.click()
                            print(f"  → Cookies acceptés via: {selector}")
                            time.sleep(0.5)
                            break
                    except Exception:
                        continue

                # Fermer modals
                for selector in [
                    "button:has-text(\"Let's plan\")",
                    "[aria-label='Close']",
                    "[aria-label='close']",
                    "button.close",
                    "button[class*='close' i]",
                ]:
                    try:
                        btn = page.query_selector(selector)
                        if btn and btn.is_visible():
                            btn.click()
                            print(f"  → Modal fermé via: {selector}")
                            time.sleep(0.5)
                            break
                    except Exception:
                        continue

                # Supprimer overlays du DOM
                removed = page.evaluate("""
                    (() => {
                        let count = 0;
                        document.querySelectorAll(
                            '[class*="modal" i], [class*="overlay" i], ' +
                            '[class*="welcome" i], [class*="cookie" i], ' +
                            '[class*="consent" i], [class*="backdrop" i]'
                        ).forEach(el => {
                            const cls = (el.className || '').toString().toLowerCase();
                            if (!cls.includes('map') && !cls.includes('leaflet')) {
                                el.remove(); count++;
                            }
                        });
                        return count;
                    })()
                """)
                if removed:
                    print(f"  → {removed} overlay(s) supprimé(s)")

                # Injecter CSS pour masquer l'UI (barre de recherche, etc.)
                page.add_style_tag(content=CLEANUP_CSS)
                time.sleep(1)

                # Attendre que les tuiles de la carte soient chargées
                time.sleep(3)

                # Screenshot en PNG (sans perte, pour superposition propre)
                page.screenshot(path=str(cache_path), type="png")
                print(f"  ✓ {cache_path} ({cache_path.stat().st_size / 1024:.0f} KB)")
                base_maps[name] = cache_path

                # Copie dans captures/_base/ pour inspection visuelle
                debug_dir = OUTPUT_DIR / "_base"
                debug_dir.mkdir(parents=True, exist_ok=True)
                debug_path = debug_dir / f"{name}_base.jpg"
                Image.open(cache_path).convert("RGB").save(
                    str(debug_path), "JPEG", quality=90)
                print(f"  → Copie inspection: {debug_path}")

            except Exception as e:
                print(f"  ✗ Erreur: {e}")
            finally:
                page.close()

        browser.close()

    return base_maps

# =============================================================================
# API — TUILES FLOW
# =============================================================================

session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS, max_retries=0)
session.mount("https://", _adapter)
session.headers["User-Agent"] = "TomTomCapture/2.0"


def fetch_tile(url, cache_key=None):
    if cache_key:
        cp = TILE_CACHE_DIR / cache_key
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
                cp = TILE_CACHE_DIR / cache_key
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


def build_flow_layer(lat, lon, zoom, width, height):
    """Télécharge et assemble les tuiles flow pour un viewport donné."""
    x0, y0, x1, y1, off_x, off_y = get_tile_grid(lat, lon, zoom, width, height)
    coords = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]

    tiles = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = []
        for tx, ty in coords:
            url = FLOW_URL.format(z=zoom, x=tx, y=ty, ts=TILE_SIZE, key=API_KEY)
            futures.append((tx, ty, pool.submit(fetch_tile, url, None)))

        for tx, ty, fut in futures:
            tiles[(tx, ty)] = fut.result()

    # Assembler
    cols, rows = x1 - x0 + 1, y1 - y0 + 1
    canvas = Image.new("RGBA", (cols * TILE_SIZE, rows * TILE_SIZE), (0, 0, 0, 0))
    for (tx, ty), img in tiles.items():
        canvas.paste(img, ((tx - x0) * TILE_SIZE, (ty - y0) * TILE_SIZE))

    # Découper au viewport exact
    return canvas.crop((off_x, off_y, off_x + width, off_y + height)), len(coords)


# =============================================================================
# API — INCIDENTS v5 + DESSIN PILLOW
# =============================================================================

def fetch_incidents(bbox):
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


def render_incidents(incidents, lat, lon, zoom, width, height):
    """Dessine les incidents directement en coordonnées viewport."""
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # Calculer l'origine pixel du viewport
    cx_px, cy_px = lat_lon_to_pixel(lat, lon, zoom)
    origin_x = cx_px - width / 2
    origin_y = cy_px - height / 2

    other_lines, closed_lines = [], []

    for inc in incidents:
        pts = []
        for coord in inc["coords"]:
            px, py = lat_lon_to_pixel(coord[1], coord[0], zoom)
            pts.append((int(px - origin_x), int(py - origin_y)))
        if len(pts) < 2:
            continue
        (closed_lines if inc["closed"] else other_lines).append(pts)

    for pts in other_lines:
        draw_dashed_line(draw, pts, COLOR_OTHER)
    for pts in closed_lines:
        draw_dashed_line(draw, pts, COLOR_CLOSED)

    n = len(closed_lines) + len(other_lines)
    print(f"    → {n} incidents ({len(closed_lines)} fermetures, {len(other_lines)} autres)")
    return canvas


# =============================================================================
# CAPTURE D'UN CYCLE (flow + incidents sur carte de base cachée)
# =============================================================================

def capture_zone(name, config, base_map_path):
    """
    Superpose flow + incidents sur la carte de base Playwright.

    Alignement clé : Playwright capture à zoom fractionnaire (ex: 9.75z).
    Les tuiles flow sont au zoom entier (10). Pour aligner :
      1. Calculer la zone géographique vue par Playwright à 9.75z/1920×1080
      2. Chercher les tuiles flow couvrant cette même zone (plus large à zoom 10)
      3. Redimensionner le résultat à 1920×1080
    """
    now = datetime.now(TIMEZONE)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M")

    lat, lon, zoom = config["lat"], config["lon"], config["zoom"]
    zoom_frac = config.get("zoom_frac", float(zoom))

    zone_dir = OUTPUT_DIR / date_str / name
    zone_dir.mkdir(parents=True, exist_ok=True)
    filename = zone_dir / f"{date_str}-{time_str}_{name}.jpg"

    print(f"\n[{name}] {now.strftime('%H:%M %Z')}")

    # ── Calcul de l'étendue géographique du screenshot Playwright ──
    # Playwright rend à zoom_frac dans un viewport 1920×1080.
    # À zoom fractionnaire, l'étendue géo est plus large que zoom entier.
    # Facteur d'expansion : combien de pixels faut-il au zoom entier
    # pour couvrir la même zone géographique que zoom_frac.
    expand = 2 ** (zoom - zoom_frac)  # >1 si arrondi haut, <1 si arrondi bas
    fetch_w = round(VIEWPORT_WIDTH * expand)
    fetch_h = round(VIEWPORT_HEIGHT * expand)
    needs_resize = abs(expand - 1.0) > 0.01

    if needs_resize:
        print(f"  zoom={zoom_frac}→{zoom}  expand={expand:.3f}  "
              f"fetch={fetch_w}×{fetch_h}→{VIEWPORT_WIDTH}×{VIEWPORT_HEIGHT}")

    # Charger la carte de base
    base = Image.open(base_map_path).convert("RGBA")

    # ── Flow tiles (étendue ajustée puis redimensionnée) ──
    flow_layer, n_tiles = build_flow_layer(lat, lon, zoom, fetch_w, fetch_h)
    if needs_resize:
        flow_layer = flow_layer.resize(
            (VIEWPORT_WIDTH, VIEWPORT_HEIGHT), Image.LANCZOS)

    # ── Incidents (bbox ajustée, dessinés puis redimensionnés) ──
    bbox = get_viewport_bbox(lat, lon, zoom, fetch_w, fetch_h)
    incidents = fetch_incidents(bbox)
    inc_layer = render_incidents(incidents, lat, lon, zoom, fetch_w, fetch_h)
    if needs_resize:
        inc_layer = inc_layer.resize(
            (VIEWPORT_WIDTH, VIEWPORT_HEIGHT), Image.LANCZOS)

    # ── Composite : base → flow → incidents ──
    composite = Image.alpha_composite(base, flow_layer)
    composite = Image.alpha_composite(composite, inc_layer)

    # Sauvegarder
    final = composite.convert("RGB")
    final.save(str(filename), "JPEG", quality=85, optimize=True)
    print(f"  ✓ {filename} ({filename.stat().st_size / 1024:.0f} KB)")

    return n_tiles


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


def clear_tile_cache():
    marker = TILE_CACHE_DIR / ".cache-date"
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    if marker.exists():
        try:
            if marker.read_text().strip() == today:
                return
        except Exception:
            pass
    if TILE_CACHE_DIR.exists():
        shutil.rmtree(TILE_CACHE_DIR)
    TILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    marker.write_text(today)

# =============================================================================
# POINTS D'ENTRÉE
# =============================================================================

def run_base_capture(parsed_zones):
    """Appelé 1× par démarrage de workflow."""
    return capture_base_maps(parsed_zones)


def run_cycle(parsed_zones, base_maps):
    """Appelé toutes les 10 min."""
    now = datetime.now(TIMEZONE)
    print(f"\n{'='*60}")
    print(f"CYCLE — {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"{'='*60}")

    total_tiles = 0
    errors = 0

    for name, config in parsed_zones.items():
        base_path = base_maps.get(name)
        if not base_path or not base_path.exists():
            print(f"[{name}] ✗ Carte de base manquante, skip")
            errors += 1
            continue
        try:
            total_tiles += capture_zone(name, config, base_path)
        except Exception as e:
            print(f"[{name}] ✗ {e}")
            errors += 1

    print(f"\nRésumé: {len(parsed_zones) - errors}/{len(parsed_zones)} zones OK")
    print(f"{counter}")
    return errors


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-only", action="store_true",
                        help="Capturer uniquement les cartes de base")
    parser.add_argument("--cycle-only", action="store_true",
                        help="Exécuter un cycle flow+incidents (base déjà en cache)")
    args = parser.parse_args()

    if not API_KEY:
        print("✗ TOMTOM_API_KEY non définie !")
        sys.exit(1)

    # Parser zones
    parsed_zones = {}
    print("Zones configurées:")
    for name, value in ZONES.items():
        try:
            cfg = parse_zone_config(value)
            parsed_zones[name] = cfg
            print(f"  ✓ {name}: zoom={cfg['zoom_frac']}→{cfg['zoom']}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")

    if not parsed_zones:
        print("✗ Aucune zone valide !")
        sys.exit(1)

    clear_tile_cache()

    if args.cycle_only:
        # Vérifier que les bases existent
        base_maps = {}
        for name in parsed_zones:
            p = BASE_CACHE_DIR / f"{name}.png"
            if p.exists():
                base_maps[name] = p
            else:
                print(f"  ⚠ Base manquante pour {name}")
        errors = run_cycle(parsed_zones, base_maps)
    elif args.base_only:
        run_base_capture(parsed_zones)
    else:
        # Mode complet : base + un cycle
        base_maps = run_base_capture(parsed_zones)
        errors = run_cycle(parsed_zones, base_maps)

    try:
        rotate_old_days()
    except Exception as e:
        print(f"[rotation] ✗ {e}")
