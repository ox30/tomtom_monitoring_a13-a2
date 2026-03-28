"""
TomTom Traffic Capture via API — GitHub Actions
================================================
Capture des zones configurées via l'API TomTom (tuiles raster).

Approche :
  1. Télécharge les tuiles carte de base  (Map Display API)
  2. Télécharge les tuiles traffic flow    (Traffic API — Raster Flow)
  3. Télécharge les tuiles incidents        (Traffic API — Raster Incidents)
  4. Superpose les 3 couches (base → flow → incidents)
  5. Découpe au viewport 1920×1080 → JPEG

Avantages vs web scraping (Playwright) :
  - Pas de Chromium (10× plus rapide, 50× plus léger)
  - Résultat déterministe et reproductible
  - Plus de gestion de popups/cookies/modals
  - Utilise les free tiles TomTom (50'000/jour)

Budget tuiles (10 min, 3 zones, 3 couches) :
  ~108 tuiles/capture × 144 captures/jour = ~15'500 tuiles/jour

Horodatage : Europe/Zurich (CET/CEST)
Structure  : captures/YYYY-MM-DD/zone_name/YYYY-MM-DD-HHMM_zone_name.jpg
Rétention  : 7 jours (configurable)
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
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

import requests
from PIL import Image

# =============================================================================
# CONFIGURATION
# =============================================================================

API_KEY = os.environ.get("TOMTOM_API_KEY", "")

# Zones de capture — collez directement l'URL de plan.tomtom.com
# Le système extrait automatiquement lat, lon et zoom depuis l'URL.
#
# Pour ajouter une zone :
#   1. Aller sur https://plan.tomtom.com
#   2. Naviguer/zoomer sur la zone souhaitée
#   3. Copier l'URL du navigateur
#   4. Ajouter une entrée ci-dessous : "nom_zone": "URL"
#
ZONES = {
    "zone_globale_A2_A13": "https://plan.tomtom.com/en/?p=46.68973,8.93561,8.55z",
    "zone_A13_Chur":       "https://plan.tomtom.com/en/?p=46.89942,9.32459,9.75z",
    "zone_Chur_Isla-T":    "https://plan.tomtom.com/en/?p=46.84086,9.45618,12.17z",
}

VIEWPORT_WIDTH  = 1920
VIEWPORT_HEIGHT = 1080
TILE_SIZE       = 512          # 512×512 px (meilleure qualité, moins de requêtes)

OUTPUT_DIR     = Path("captures")
CACHE_DIR      = Path(".tile-cache")
TIMEZONE       = ZoneInfo("Europe/Zurich")
RETENTION_DAYS = 7

# Endpoints TomTom API
BASE_URL = (
    "https://api.tomtom.com/map/1/tile/basic/main"
    "/{z}/{x}/{y}.png?tileSize={ts}&key={key}"
)
FLOW_URL = (
    "https://api.tomtom.com/traffic/map/4/tile/flow/relative"
    "/{z}/{x}/{y}.png?tileSize={ts}&key={key}"
)
# Les tuiles incidents ne supportent pas tileSize → 256×256 par défaut
# Elles seront upscalées à 512 lors du compositing
INCIDENTS_URL = (
    "https://api.tomtom.com/traffic/map/4/tile/incidents/s3"
    "/{z}/{x}/{y}.png?key={key}"
)

MAX_RETRIES     = 2
REQUEST_TIMEOUT = 15
MAX_WORKERS     = 8            # Requêtes parallèles

# =============================================================================
# COMPTEUR DE TUILES (suivi du budget API)
# =============================================================================

class TileCounter:
    """Compteur simple pour suivre l'utilisation de l'API."""
    def __init__(self):
        self.fetched = 0    # Tuiles téléchargées (API calls réels)
        self.cached  = 0    # Tuiles servies depuis le cache

    def add_fetch(self):
        self.fetched += 1

    def add_cache(self):
        self.cached += 1

    @property
    def total(self):
        return self.fetched + self.cached

    def __str__(self):
        return f"API={self.fetched} cache={self.cached} total={self.total}"

counter = TileCounter()

# =============================================================================
# PARSEUR D'URL plan.tomtom.com
# =============================================================================

def parse_tomtom_url(url: str) -> dict:
    """
    Extrait lat, lon, zoom depuis une URL plan.tomtom.com.

    Formats supportés :
      - https://plan.tomtom.com/en/?p=46.68973,8.93561,8.55z
      - https://plan.tomtom.com/?p=46.68973,8.93561,8.55z
      - https://plan.tomtom.com/en/?p=46.68973,8.93561,8z

    Le zoom est arrondi à l'entier le plus proche (l'API tuiles
    n'accepte que des zooms entiers).

    Returns:
        {"lat": float, "lon": float, "zoom": int}
    """
    # Extraire le paramètre 'p' de l'URL
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    if "p" in params:
        p_value = params["p"][0]
    else:
        # Fallback : chercher le pattern directement dans l'URL
        match = re.search(r"p=([^&]+)", url)
        if not match:
            raise ValueError(f"Impossible de trouver le paramètre 'p=' dans l'URL: {url}")
        p_value = match.group(1)

    # Parser "lat,lon,zoomz" — le 'z' final indique le zoom
    match = re.match(r"^(-?[\d.]+),(-?[\d.]+),([\d.]+)z?$", p_value)
    if not match:
        raise ValueError(
            f"Format inattendu pour le paramètre p='{p_value}'\n"
            f"  Attendu: lat,lon,zoomz (ex: 46.68973,8.93561,8.55z)"
        )

    lat  = float(match.group(1))
    lon  = float(match.group(2))
    zoom = round(float(match.group(3)))  # Arrondi à l'entier le plus proche

    # Validation
    if not (-90 <= lat <= 90):
        raise ValueError(f"Latitude hors limites: {lat}")
    if not (-180 <= lon <= 180):
        raise ValueError(f"Longitude hors limites: {lon}")
    if not (0 <= zoom <= 22):
        raise ValueError(f"Zoom hors limites: {zoom} (doit être entre 0 et 22)")

    return {"lat": lat, "lon": lon, "zoom": zoom}


def parse_zone_config(value) -> dict:
    """
    Accepte soit une URL (str), soit un dict {lat, lon, zoom} déjà parsé.
    Permet de mixer les deux formats dans ZONES si besoin.
    """
    if isinstance(value, str):
        return parse_tomtom_url(value)
    elif isinstance(value, dict):
        return value
    else:
        raise TypeError(f"Zone config invalide: {type(value)} — attendu str (URL) ou dict")


# =============================================================================
# CALCULS TUILES (Spherical Mercator — EPSG:3857)
# =============================================================================

def lat_lon_to_tile(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Convertit lat/lon → coordonnées de tuile fractionnaires au zoom donné."""
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def get_tile_grid(
    lat: float, lon: float, zoom: int,
    width: int = VIEWPORT_WIDTH,
    height: int = VIEWPORT_HEIGHT,
    tile_size: int = TILE_SIZE,
) -> tuple[int, int, int, int, int, int]:
    """
    Calcule la grille de tuiles nécessaire pour couvrir le viewport.

    Retourne:
        (x_start, y_start, x_end, y_end, offset_x, offset_y)
        - x/y_start..end : indices de tuiles (inclus)
        - offset_x/y     : pixels à rogner en haut-gauche pour centrer
    """
    cx, cy = lat_lon_to_tile(lat, lon, zoom)

    # Position du centre en pixels dans l'espace global des tuiles
    cpx = cx * tile_size
    cpy = cy * tile_size

    # Coin supérieur-gauche du viewport
    tl_px = cpx - width / 2
    tl_py = cpy - height / 2

    # Indices de tuiles (inclusifs)
    x_start = int(math.floor(tl_px / tile_size))
    y_start = int(math.floor(tl_py / tile_size))
    x_end   = int(math.floor((tl_px + width - 1) / tile_size))
    y_end   = int(math.floor((tl_py + height - 1) / tile_size))

    # Offset pixel pour le crop final
    offset_x = int(tl_px - x_start * tile_size)
    offset_y = int(tl_py - y_start * tile_size)

    # Clamp aux limites de la grille
    max_tile = 2 ** zoom - 1
    x_start = max(0, x_start)
    y_start = max(0, y_start)
    x_end   = min(max_tile, x_end)
    y_end   = min(max_tile, y_end)

    return x_start, y_start, x_end, y_end, offset_x, offset_y

# =============================================================================
# TÉLÉCHARGEMENT DES TUILES
# =============================================================================

session = requests.Session()
adapter = requests.adapters.HTTPAdapter(
    pool_connections=MAX_WORKERS,
    pool_maxsize=MAX_WORKERS,
    max_retries=0,  # On gère les retries manuellement
)
session.mount("https://", adapter)
session.headers.update({"User-Agent": "TomTomCapture/2.0"})


def fetch_tile(url: str, cache_key: str | None = None) -> Image.Image:
    """
    Télécharge une tuile PNG depuis l'API TomTom.

    Args:
        url:       URL complète de la tuile
        cache_key: Chemin relatif dans CACHE_DIR (None = pas de cache)

    Returns:
        Image RGBA (tuile transparente en cas d'échec)
    """
    # Vérifier le cache
    if cache_key:
        cache_path = CACHE_DIR / cache_key
        if cache_path.exists():
            counter.add_cache()
            return Image.open(cache_path).convert("RGBA")

    # Télécharger avec retry
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            img = Image.open(BytesIO(r.content)).convert("RGBA")
            counter.add_fetch()

            # Sauvegarder en cache si demandé
            if cache_key:
                cache_path = CACHE_DIR / cache_key
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                img.save(str(cache_path), "PNG")

            return img

        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(1.5 * (attempt + 1))
            else:
                print(f"    ✗ Tuile échouée après {MAX_RETRIES+1} tentatives: {e}")
                counter.add_fetch()  # On compte quand même l'appel API
                return Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))

    # Ne devrait jamais arriver
    return Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))

# =============================================================================
# ASSEMBLAGE DES COUCHES
# =============================================================================

def build_layer(
    tiles: dict[tuple[int, int], Image.Image],
    x_start: int, y_start: int,
    x_end: int, y_end: int,
    target_size: int = TILE_SIZE,
) -> Image.Image:
    """
    Assemble les tuiles en une image unique.
    Redimensionne automatiquement les tuiles qui ne font pas target_size
    (cas des tuiles incidents en 256×256).
    """
    cols = x_end - x_start + 1
    rows = y_end - y_start + 1
    canvas = Image.new("RGBA", (cols * target_size, rows * target_size), (0, 0, 0, 0))

    for (tx, ty), img in tiles.items():
        px = (tx - x_start) * target_size
        py = (ty - y_start) * target_size

        # Redimensionner si nécessaire (tuiles incidents = 256×256)
        if img.size[0] != target_size or img.size[1] != target_size:
            img = img.resize((target_size, target_size), Image.LANCZOS)

        canvas.paste(img, (px, py))

    return canvas

# =============================================================================
# CAPTURE D'UNE ZONE
# =============================================================================

def capture_zone(name: str, config: dict) -> int:
    """
    Capture complète d'une zone : télécharge 3 couches, compose, sauvegarde.

    Returns:
        Nombre de tuiles utilisées (pour le suivi du budget)
    """
    now = datetime.now(TIMEZONE)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M")

    lat, lon, zoom = config["lat"], config["lon"], config["zoom"]

    # Dossier de sortie
    zone_dir = OUTPUT_DIR / date_str / name
    zone_dir.mkdir(parents=True, exist_ok=True)
    filename = zone_dir / f"{date_str}-{time_str}_{name}.jpg"

    print(f"\n{'='*60}")
    print(f"[{name}] {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"[{name}] zoom={zoom}  center=({lat:.5f}, {lon:.5f})")

    # Calculer la grille de tuiles
    x0, y0, x1, y1, off_x, off_y = get_tile_grid(lat, lon, zoom)
    tile_coords = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    n_tiles = len(tile_coords)
    print(f"[{name}] Grille: {x1-x0+1}×{y1-y0+1} = {n_tiles} tuiles/couche"
          f"  (total: {n_tiles*3} tuiles)")

    # --- Télécharger toutes les tuiles en parallèle ---
    base_tiles = {}
    flow_tiles = {}
    incident_tiles = {}

    futures = []  # (layer, tx, ty, future)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for tx, ty in tile_coords:
            # Carte de base — cachée pour la journée (ne change pas)
            base_url = BASE_URL.format(z=zoom, x=tx, y=ty, ts=TILE_SIZE, key=API_KEY)
            cache_key = f"base/{date_str}/{zoom}/{tx}_{ty}.png"
            futures.append(("base", tx, ty,
                            pool.submit(fetch_tile, base_url, cache_key)))

            # Traffic Flow — temps réel, jamais caché
            flow_url = FLOW_URL.format(z=zoom, x=tx, y=ty, ts=TILE_SIZE, key=API_KEY)
            futures.append(("flow", tx, ty,
                            pool.submit(fetch_tile, flow_url, None)))

            # Incidents — temps réel, jamais caché
            inc_url = INCIDENTS_URL.format(z=zoom, x=tx, y=ty, key=API_KEY)
            futures.append(("incidents", tx, ty,
                            pool.submit(fetch_tile, inc_url, None)))

        # Collecter les résultats
        for layer, tx, ty, future in futures:
            img = future.result()
            if layer == "base":
                base_tiles[(tx, ty)] = img
            elif layer == "flow":
                flow_tiles[(tx, ty)] = img
            else:
                incident_tiles[(tx, ty)] = img

    # --- Assembler les couches ---
    print(f"[{name}] Assemblage des couches...")
    base_layer     = build_layer(base_tiles,     x0, y0, x1, y1)
    flow_layer     = build_layer(flow_tiles,     x0, y0, x1, y1)
    incident_layer = build_layer(incident_tiles, x0, y0, x1, y1)

    # Composite : base → flow (transparent) → incidents (transparent)
    composite = Image.alpha_composite(base_layer, flow_layer)
    composite = Image.alpha_composite(composite, incident_layer)

    # Découper au viewport exact
    cropped = composite.crop((off_x, off_y, off_x + VIEWPORT_WIDTH, off_y + VIEWPORT_HEIGHT))

    # Sauvegarder en JPEG
    final = cropped.convert("RGB")
    final.save(str(filename), "JPEG", quality=85, optimize=True)

    size_kb = filename.stat().st_size / 1024
    print(f"[{name}] ✓ {filename} ({size_kb:.0f} KB)")

    return n_tiles * 3


# =============================================================================
# MAINTENANCE
# =============================================================================

def rotate_old_days():
    """Supprime les dossiers de captures plus vieux que RETENTION_DAYS."""
    if not OUTPUT_DIR.exists():
        return

    cutoff = datetime.now(TIMEZONE).date() - timedelta(days=RETENTION_DAYS)
    for day_dir in sorted(OUTPUT_DIR.iterdir()):
        if not day_dir.is_dir() or day_dir.name.startswith("_"):
            continue
        try:
            folder_date = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if folder_date < cutoff:
            shutil.rmtree(day_dir)
            print(f"[rotation] Supprimé: {day_dir.name}")


def clear_stale_cache():
    """Réinitialise le cache des tuiles de base chaque jour."""
    marker = CACHE_DIR / ".cache-date"
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    if marker.exists():
        try:
            if marker.read_text().strip() == today:
                return  # Cache encore valide
        except Exception:
            pass

    # Nouveau jour → vider le cache
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    marker.write_text(today)
    print("[cache] Cache tuiles de base réinitialisé pour aujourd'hui")


# =============================================================================
# POINT D'ENTRÉE
# =============================================================================

if __name__ == "__main__":
    if not API_KEY:
        print("✗ Variable TOMTOM_API_KEY non définie !")
        print("  → Ajouter dans GitHub > Settings > Secrets > TOMTOM_API_KEY")
        sys.exit(1)

    # Parser toutes les zones (URL → lat/lon/zoom)
    parsed_zones = {}
    print("Zones configurées:")
    for name, value in ZONES.items():
        try:
            config = parse_zone_config(value)
            parsed_zones[name] = config
            print(f"  ✓ {name}: lat={config['lat']:.5f} lon={config['lon']:.5f} zoom={config['zoom']}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")

    if not parsed_zones:
        print("✗ Aucune zone valide configurée !")
        sys.exit(1)

    clear_stale_cache()

    total_tiles = 0
    errors = 0

    for zone_name, zone_config in parsed_zones.items():
        try:
            total_tiles += capture_zone(zone_name, zone_config)
        except Exception as e:
            print(f"[{zone_name}] ✗ Erreur: {e}")
            errors += 1

    # Résumé
    print(f"\n{'='*60}")
    print(f"Résumé: {len(parsed_zones) - errors}/{len(parsed_zones)} zones OK")
    print(f"Tuiles: {counter}")
    print(f"Budget journalier estimé: ~{total_tiles * 144} / 50'000")
    print(f"{'='*60}")

    try:
        rotate_old_days()
    except Exception as e:
        print(f"[rotation] ✗ Erreur: {e}")
