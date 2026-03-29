"""
TomTom Traffic Capture — Vector Flow Edition
=============================================
Capture automatique du trafic via l'API TomTom, orchestrée par GitHub Actions.

Architecture (3 couches superposées) :
  1. Carte de base   → API Static Image (une seule requête HTTP, pas de navigateur)
  2. Traffic Flow     → Vector Flow Tiles (.pbf) — filtrage par type de route,
                        rendu Pillow avec la charte visuelle relative0 de plan.tomtom.com
  3. Incidents        → API Incident Details v5 + dessin Pillow (pointillés)

Avantages vs raster flow tiles :
  - Filtrage par catégorie de route (motorway, international, major…)
  - Épaisseur de ligne proportionnelle au type de route
  - Contour sombre autour de chaque segment (comme plan.tomtom.com)
  - Contrôle total sur les couleurs et le style

Dépendances : requests, Pillow, mapbox-vector-tile
"""

import os
import sys
import math
import json
import struct
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from io import BytesIO
from PIL import Image, ImageDraw

# ─── Protobuf minimal parser ─────────────────────────────────────────────────
# Parseur protobuf léger intégré — pas de dépendance externe requise.
# Supporte le format Mapbox Vector Tile (MVT) utilisé par TomTom.

def _decode_varint(data, pos):
    """Décode un varint protobuf."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, pos
        shift += 7
    raise ValueError("Varint tronqué")


def _decode_zigzag(n):
    """Décode un entier signé zigzag."""
    return (n >> 1) ^ -(n & 1)


def _parse_protobuf(data):
    """Parse un message protobuf en dict {field_number: [values]}."""
    fields = {}
    pos = 0
    while pos < len(data):
        tag, pos = _decode_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # Varint
            val, pos = _decode_varint(data, pos)
        elif wire_type == 1:  # 64-bit
            val = struct.unpack('<d', data[pos:pos+8])[0]
            pos += 8
        elif wire_type == 2:  # Length-delimited
            length, pos = _decode_varint(data, pos)
            val = data[pos:pos+length]
            pos += length
        elif wire_type == 5:  # 32-bit
            val = struct.unpack('<f', data[pos:pos+4])[0]
            pos += 4
        else:
            raise ValueError(f"Wire type inconnu: {wire_type}")

        fields.setdefault(field_num, []).append(val)
    return fields


def _decode_packed_uint32(data):
    """Décode un champ packed repeated uint32."""
    values = []
    pos = 0
    while pos < len(data):
        val, pos = _decode_varint(data, pos)
        values.append(val)
    return values


def _decode_geometry(geom_data):
    """Décode la géométrie MVT en liste de lignes [(x,y), ...]."""
    integers = _decode_packed_uint32(geom_data)
    lines = []
    current_line = []
    cx, cy = 0, 0
    i = 0

    while i < len(integers):
        cmd_int = integers[i]
        cmd = cmd_int & 0x07
        count = cmd_int >> 3
        i += 1

        if cmd == 1:  # MoveTo
            for _ in range(count):
                dx = _decode_zigzag(integers[i])
                dy = _decode_zigzag(integers[i + 1])
                cx += dx
                cy += dy
                i += 2
                if current_line:
                    lines.append(current_line)
                current_line = [(cx, cy)]
        elif cmd == 2:  # LineTo
            for _ in range(count):
                dx = _decode_zigzag(integers[i])
                dy = _decode_zigzag(integers[i + 1])
                cx += dx
                cy += dy
                i += 2
                current_line.append((cx, cy))
        elif cmd == 7:  # ClosePath
            if current_line and len(current_line) > 1:
                current_line.append(current_line[0])

    if current_line:
        lines.append(current_line)
    return lines


def parse_mvt_tile(data):
    """
    Parse une tuile MVT et retourne les features du layer 'Traffic flow'.

    Retourne: liste de dicts avec 'geometry' (lignes), 'tags' (dict de propriétés)
    """
    if not data or len(data) < 2:
        return []

    tile = _parse_protobuf(data)
    features_out = []

    # Field 3 = layers dans Tile
    for layer_data in tile.get(3, []):
        layer = _parse_protobuf(layer_data)

        # Field 1 = name
        name = layer.get(1, [b''])[0]
        if isinstance(name, bytes):
            name = name.decode('utf-8', errors='ignore')

        # On ne traite que le layer "Traffic flow"
        if name != "Traffic flow":
            continue

        # Field 5 = extent (défaut 4096)
        extent = layer.get(5, [4096])[0]

        # Field 3 = keys (array de strings)
        keys = []
        for k in layer.get(3, []):
            keys.append(k.decode('utf-8', errors='ignore') if isinstance(k, bytes) else str(k))

        # Field 4 = values (array de TileValue)
        values = []
        for v_data in layer.get(4, []):
            v_fields = _parse_protobuf(v_data)
            # TileValue: 1=string, 2=float, 3=double, 4=int64, 5=uint64, 6=sint64, 7=bool
            if 1 in v_fields:
                val = v_fields[1][0]
                values.append(val.decode('utf-8', errors='ignore') if isinstance(val, bytes) else str(val))
            elif 2 in v_fields:
                values.append(v_fields[2][0])
            elif 3 in v_fields:
                values.append(v_fields[3][0])
            elif 4 in v_fields:
                values.append(v_fields[4][0])
            elif 5 in v_fields:
                values.append(v_fields[5][0])
            elif 6 in v_fields:
                values.append(_decode_zigzag(v_fields[6][0]))
            elif 7 in v_fields:
                values.append(bool(v_fields[7][0]))
            else:
                values.append(None)

        # Field 2 = features
        for feat_data in layer.get(2, []):
            feat = _parse_protobuf(feat_data)

            # Field 2 = tags (packed uint32)
            tags_raw = _decode_packed_uint32(feat.get(2, [b''])[0]) if 2 in feat and isinstance(feat[2][0], bytes) else []
            props = {}
            for j in range(0, len(tags_raw) - 1, 2):
                k_idx = tags_raw[j]
                v_idx = tags_raw[j + 1]
                if k_idx < len(keys) and v_idx < len(values):
                    props[keys[k_idx]] = values[v_idx]

            # Field 4 = geometry (packed uint32)
            geom_raw = feat.get(4, [b''])[0]
            if isinstance(geom_raw, bytes) and len(geom_raw) > 0:
                geometry = _decode_geometry(geom_raw)
            else:
                geometry = []

            if geometry:
                features_out.append({
                    'geometry': geometry,
                    'tags': props,
                    'extent': extent,
                })

    return features_out


# ─── Configuration ────────────────────────────────────────────────────────────

# Zones — coller directement l'URL de plan.tomtom.com
ZONES = {
    "zone_globale_A2_A13": "https://plan.tomtom.com/en/?p=46.68973,8.93561,8.55z",
    "zone_A13_Chur":       "https://plan.tomtom.com/en/?p=46.89942,9.32459,9.75z",
    "zone_Chur_Isla-T":    "https://plan.tomtom.com/en/?p=46.84086,9.45618,12.17z",
}

VIEWPORT_WIDTH  = 1920
VIEWPORT_HEIGHT = 1080
TILE_SIZE       = 512       # Taille des tuiles vectorielles (en pixels de rendu)
RETENTION_DAYS  = 7
OUTPUT_DIR      = Path("captures")
CACHE_DIR       = Path(".base-cache")

# Filtrage des types de route par niveau de zoom
# Plus le zoom est faible (vue large), moins on affiche de routes
ROAD_TYPES_BY_ZOOM = {
    8:  [0],            # Motorway uniquement
    9:  [0, 1],         # + International
    10: [0, 1, 2],      # + Major
    11: [0, 1, 2, 3],   # + Secondary
    12: [0, 1, 2, 3],   # + Secondary
    13: [0, 1, 2, 3, 4],
    14: [0, 1, 2, 3, 4, 5],
    15: [0, 1, 2, 3, 4, 5, 6],
}

# Épaisseur des lignes par type de route (outline, main)
# Reproduit la hiérarchie visuelle de plan.tomtom.com
LINE_WIDTH = {
    "Motorway":         (8, 5),
    "International road": (7, 4),
    "Major road":       (6, 4),
    "Secondary road":   (5, 3),
    "Connecting road":  (4, 3),
    "Major local road": (4, 2),
    "Local road":       (3, 2),
    "Minor local road": (3, 2),
    "Non public road":  (2, 1),
    "Parking road":     (2, 1),
}
DEFAULT_WIDTH = (4, 3)

# ─── Charte visuelle TomTom relative0 ────────────────────────────────────────
# Couleurs exactes extraites de la documentation officielle TomTom
# https://developer.tomtom.com/traffic-api/documentation/traffic-flow/raster-flow-tiles

# (outline_color, main_color) — RGBA
TRAFFIC_COLORS = {
    "closed":       ((102, 102, 102, 255), (193, 39, 45, 255)),    # Route fermée
    "very_slow":    ((165, 7, 4, 255),     (231, 7, 4, 255)),      # < 15% free-flow
    "slow":         ((223, 75, 21, 255),   (241, 130, 55, 255)),   # 15-35%
    "moderate":     ((232, 123, 61, 255),  (241, 191, 64, 255)),   # 35-75%
    "free":         ((36, 87, 35, 255),    (46, 171, 48, 255)),    # ≥ 75%
}


def get_traffic_category(tags):
    """Détermine la catégorie de trafic à partir des tags du segment."""
    if tags.get("road_closure"):
        return "closed"
    level = tags.get("traffic_level", 1.0)
    if isinstance(level, str):
        try:
            level = float(level)
        except ValueError:
            level = 1.0
    if level < 0.15:
        return "very_slow"
    elif level < 0.35:
        return "slow"
    elif level < 0.75:
        return "moderate"
    else:
        return "free"


# Dessin incidents (inchangé — API v5 + Pillow)
COLOR_CLOSED = (190, 25, 25, 255)
COLOR_OTHER  = (110, 110, 110, 255)
INCIDENT_LINE_WIDTH = 5
DASH_ON      = 6
DASH_OFF     = 5

# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_zone_url(url):
    """Extrait lat, lon, zoom depuis une URL plan.tomtom.com."""
    import re
    m = re.search(r'p=([-\d.]+),([-\d.]+),([-\d.]+)z', url)
    if not m:
        raise ValueError(f"URL invalide: {url}")
    lat, lon, zoom = float(m.group(1)), float(m.group(2)), float(m.group(3))
    zoom = int(round(zoom))
    return lat, lon, zoom


def lat_lon_to_tile(lat, lon, zoom):
    """Convertit lat/lon en coordonnées de tuile (x, y) flottantes."""
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def get_tile_grid(lat, lon, zoom, width, height, tile_size):
    """
    Calcule la grille de tuiles nécessaire pour couvrir le viewport.
    Retourne (tiles_info, origin_offset_x, origin_offset_y).
    tiles_info = [(tile_x, tile_y, pixel_offset_x, pixel_offset_y), ...]
    """
    center_tx, center_ty = lat_lon_to_tile(lat, lon, zoom)

    # Pixel du centre dans l'espace global des tuiles
    center_px = center_tx * tile_size
    center_py = center_ty * tile_size

    # Coin supérieur gauche du viewport en pixels globaux
    origin_px = center_px - width / 2
    origin_py = center_py - height / 2

    # Range de tuiles
    tile_x_min = int(math.floor(origin_px / tile_size))
    tile_x_max = int(math.floor((origin_px + width - 1) / tile_size))
    tile_y_min = int(math.floor(origin_py / tile_size))
    tile_y_max = int(math.floor((origin_py + height - 1) / tile_size))

    tiles = []
    for ty in range(tile_y_min, tile_y_max + 1):
        for tx in range(tile_x_min, tile_x_max + 1):
            px_offset = tx * tile_size - origin_px
            py_offset = ty * tile_size - origin_py
            tiles.append((tx, ty, px_offset, py_offset))

    return tiles, origin_px, origin_py


# ─── API calls ────────────────────────────────────────────────────────────────

counter = 0  # Compteur de requêtes API


def api_get(url, binary=False):
    """Requête GET avec gestion d'erreurs."""
    global counter
    counter += 1
    resp = requests.get(url, timeout=30)
    if resp.status_code == 200:
        return resp.content if binary else resp.json()
    print(f"  ⚠ HTTP {resp.status_code} pour {url[:100]}...")
    return None


def download_base_image(lat, lon, zoom, width, height, api_key):
    """
    Télécharge la carte de base via l'API Static Image de TomTom.
    Une seule requête HTTP — pas de navigateur, pas d'assemblage de tuiles.
    """
    # Limiter à 8192x8192 (max API)
    w = min(width, 8192)
    h = min(height, 8192)

    url = (
        f"https://api.tomtom.com/map/1/staticimage"
        f"?key={api_key}"
        f"&center={lon},{lat}"
        f"&zoom={zoom}"
        f"&width={w}&height={h}"
        f"&format=png"
        f"&layer=basic&style=main"
        f"&language=de-DE"
    )
    print(f"  📍 Base map: {w}×{h} zoom={zoom}")
    data = api_get(url, binary=True)
    if data:
        img = Image.open(BytesIO(data)).convert("RGBA")
        # Redimensionner si nécessaire
        if img.size != (width, height):
            img = img.resize((width, height), Image.LANCZOS)
        return img
    return None


def download_vector_flow(lat, lon, zoom, width, height, api_key):
    """
    Télécharge les tuiles vectorielles de trafic flow (.pbf),
    parse les segments et les dessine avec la charte relative0 de TomTom.
    """
    # Déterminer les types de route à afficher pour ce zoom
    road_types = ROAD_TYPES_BY_ZOOM.get(zoom, [0, 1, 2, 3])
    road_types_param = "[" + ",".join(str(r) for r in road_types) + "]"

    tiles, origin_px, origin_py = get_tile_grid(lat, lon, zoom, width, height, TILE_SIZE)

    # Image transparente pour le flow
    flow_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(flow_img)

    n_tiles = len(tiles)
    n_features = 0
    n_downloaded = 0

    for tile_x, tile_y, px_off, py_off in tiles:
        # Clamp tile coords
        max_tile = 2 ** zoom - 1
        if tile_y < 0 or tile_y > max_tile:
            continue

        tx = tile_x % (max_tile + 1)

        url = (
            f"https://api.tomtom.com/traffic/map/4/tile/flow"
            f"/relative/{zoom}/{tx}/{tile_y}.pbf"
            f"?key={api_key}"
            f"&roadTypes={road_types_param}"
            f"&tags=[road_type,traffic_level,road_closure]"
        )

        data = api_get(url, binary=True)
        if not data:
            continue
        n_downloaded += 1

        features = parse_mvt_tile(data)

        # Trier : dessiner les routes fluides d'abord, congestionnées par-dessus
        def sort_key(f):
            cat = get_traffic_category(f['tags'])
            order = {"free": 0, "moderate": 1, "slow": 2, "very_slow": 3, "closed": 4}
            return order.get(cat, 0)

        features.sort(key=sort_key)

        for feat in features:
            tags = feat['tags']
            extent = feat.get('extent', 4096)
            category = get_traffic_category(tags)
            outline_color, main_color = TRAFFIC_COLORS[category]

            # Épaisseur selon le type de route
            road_type = tags.get('road_type', '')
            outline_w, main_w = LINE_WIDTH.get(road_type, DEFAULT_WIDTH)

            for line in feat['geometry']:
                if len(line) < 2:
                    continue

                # Convertir coords tuile (0-extent) en pixels viewport
                pixel_line = []
                for tx_coord, ty_coord in line:
                    px = px_off + (tx_coord / extent) * TILE_SIZE
                    py = py_off + (ty_coord / extent) * TILE_SIZE
                    pixel_line.append((px, py))

                # Simplifier : ignorer les segments hors viewport (avec marge)
                margin = 50
                all_outside = all(
                    x < -margin or x > width + margin or y < -margin or y > height + margin
                    for x, y in pixel_line
                )
                if all_outside:
                    continue

                n_features += 1

                # Dessiner : contour d'abord, puis trait principal
                coords = [(int(round(x)), int(round(y))) for x, y in pixel_line]

                if len(coords) >= 2:
                    # Outline (plus large, couleur sombre)
                    draw.line(coords, fill=outline_color, width=outline_w, joint="curve")
                    # Main line (plus fine, couleur vive)
                    draw.line(coords, fill=main_color, width=main_w, joint="curve")

    print(f"  🚗 Flow: {n_downloaded}/{n_tiles} tuiles, {n_features} segments"
          f" (roadTypes={road_types})")
    return flow_img


def download_incidents(lat, lon, zoom, width, height, api_key):
    """
    Télécharge les incidents via l'API v5 et les dessine en pointillés.
    Rouge = fermetures, Gris = autres incidents.
    """
    # Bounding box depuis le viewport
    tiles, origin_px, origin_py = get_tile_grid(lat, lon, zoom, width, height, TILE_SIZE)

    # Calculer la bbox en lat/lon
    n = 2 ** zoom
    min_px = origin_px
    max_px = origin_px + width
    min_py = origin_py
    max_py = origin_py + height

    def px_to_lon(px):
        return (px / TILE_SIZE) / n * 360.0 - 180.0

    def py_to_lat(py):
        lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * (py / TILE_SIZE) / n)))
        return math.degrees(lat_rad)

    min_lon = px_to_lon(min_px)
    max_lon = px_to_lon(max_px)
    min_lat = py_to_lat(max_py)  # y inversé
    max_lat = py_to_lat(min_py)

    bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"

    url = (
        f"https://api.tomtom.com/traffic/services/5/incidentDetails"
        f"?key={api_key}"
        f"&bbox={bbox}"
        f"&fields={{incidents{{type,geometry{{type,coordinates}},properties{{iconCategory}}}}}}"
        f"&language=en-US"
        f"&categoryFilter=0,1,2,3,4,5,6,7,8,9,10,11,13,14"
    )

    data = api_get(url)
    if not data or "incidents" not in data:
        print(f"  ⚠ Pas d'incidents")
        return Image.new("RGBA", (width, height), (0, 0, 0, 0))

    inc_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(inc_img)
    count = 0

    for inc in data.get("incidents", []):
        geom = inc.get("geometry", {})
        props = inc.get("properties", {})
        icon_cat = props.get("iconCategory", -1)

        coords_raw = geom.get("coordinates", [])
        if geom.get("type") == "Point":
            coords_raw = [coords_raw]

        if not coords_raw or not isinstance(coords_raw[0], list):
            continue

        # Couleur selon le type
        color = COLOR_CLOSED if icon_cat == 8 else COLOR_OTHER

        # Convertir lon/lat en pixels viewport
        pixel_pts = []
        for coord in coords_raw:
            if len(coord) >= 2:
                clon, clat = coord[0], coord[1]
                tx, ty = lat_lon_to_tile(clat, clon, zoom)
                px = tx * TILE_SIZE - origin_px
                py = ty * TILE_SIZE - origin_py
                pixel_pts.append((int(round(px)), int(round(py))))

        if len(pixel_pts) < 2:
            continue

        # Dessiner en pointillés
        for i in range(len(pixel_pts) - 1):
            x0, y0 = pixel_pts[i]
            x1, y1 = pixel_pts[i + 1]
            dx, dy = x1 - x0, y1 - y0
            length = math.hypot(dx, dy)
            if length < 1:
                continue

            ux, uy = dx / length, dy / length
            pos = 0
            drawing = True

            while pos < length:
                seg = DASH_ON if drawing else DASH_OFF
                end = min(pos + seg, length)
                if drawing:
                    sx = int(round(x0 + ux * pos))
                    sy = int(round(y0 + uy * pos))
                    ex = int(round(x0 + ux * end))
                    ey = int(round(y0 + uy * end))
                    draw.line([(sx, sy), (ex, ey)], fill=color, width=INCIDENT_LINE_WIDTH)
                pos = end
                drawing = not drawing

        count += 1

    print(f"  ⚠ Incidents: {count} dessinés")
    return inc_img


# ─── Capture d'une zone ──────────────────────────────────────────────────────

def capture_zone(zone_name, zone_url, api_key, now):
    """Capture complète d'une zone : base + flow + incidents."""
    lat, lon, zoom = parse_zone_url(zone_url)
    print(f"\n{'─'*60}")
    print(f"[{zone_name}] lat={lat} lon={lon} zoom={zoom}")

    # 1. Carte de base (cachée — ne change pas souvent)
    cache_path = CACHE_DIR / f"{zone_name}_z{zoom}.png"
    cache_age_ok = False
    if cache_path.exists():
        age = now.timestamp() - cache_path.stat().st_mtime
        if age < 6 * 3600:  # 6h
            cache_age_ok = True

    if cache_age_ok:
        print(f"  📦 Base map: cache OK ({cache_path})")
        base_img = Image.open(cache_path).convert("RGBA")
    else:
        base_img = download_base_image(lat, lon, zoom, VIEWPORT_WIDTH, VIEWPORT_HEIGHT, api_key)
        if base_img is None:
            print(f"  ✗ Impossible de télécharger la carte de base")
            return 0
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        base_img.save(str(cache_path), "PNG")
        print(f"  💾 Base map cachée: {cache_path}")

    # 2. Traffic Flow (vector tiles)
    flow_img = download_vector_flow(lat, lon, zoom, VIEWPORT_WIDTH, VIEWPORT_HEIGHT, api_key)

    # 3. Incidents
    inc_img = download_incidents(lat, lon, zoom, VIEWPORT_WIDTH, VIEWPORT_HEIGHT, api_key)

    # 4. Composer l'image finale
    composite = base_img.copy()
    composite = Image.alpha_composite(composite, flow_img)
    composite = Image.alpha_composite(composite, inc_img)

    # 5. Sauvegarder
    date_dir = OUTPUT_DIR / now.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{zone_name}_{now.strftime('%H%M')}.jpg"
    out_path = date_dir / filename
    composite.convert("RGB").save(str(out_path), "JPEG", quality=88)
    size_kb = out_path.stat().st_size / 1024
    print(f"  ✅ {out_path} ({size_kb:.0f} KB)")

    # Copie pour inspection de la carte de base
    debug_dir = OUTPUT_DIR / "_base"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_path = debug_dir / f"{zone_name}_base.jpg"
    if not debug_path.exists() or not cache_age_ok:
        base_img.convert("RGB").save(str(debug_path), "JPEG", quality=90)

    return counter


def rotate_old_days():
    """Supprime les captures de plus de RETENTION_DAYS jours."""
    if not OUTPUT_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    for day_dir in OUTPUT_DIR.iterdir():
        if day_dir.is_dir() and day_dir.name.startswith("20"):
            try:
                dir_date = datetime.strptime(day_dir.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if dir_date < cutoff:
                    import shutil
                    shutil.rmtree(day_dir)
                    print(f"  🗑 Supprimé: {day_dir}")
            except ValueError:
                pass


def clear_stale_cache():
    """Supprime les caches de base de plus de 24h."""
    if not CACHE_DIR.exists():
        return
    now = datetime.now(timezone.utc).timestamp()
    for f in CACHE_DIR.glob("*.png"):
        if now - f.stat().st_mtime > 24 * 3600:
            f.unlink()
            print(f"  🗑 Cache expiré: {f}")


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    api_key = os.environ.get("TOMTOM_API_KEY", "")
    if not api_key:
        print("✗ TOMTOM_API_KEY non définie")
        print("  → export TOMTOM_API_KEY='votre_clé'")
        print("  → ou Settings > Secrets > TOMTOM_API_KEY dans GitHub")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    print(f"═══ TomTom Vector Flow Capture — {now.strftime('%Y-%m-%d %H:%M UTC')} ═══")

    # Afficher les zones configurées
    print("\nZones configurées:")
    for name, url in ZONES.items():
        lat, lon, zoom = parse_zone_url(url)
        road_types = ROAD_TYPES_BY_ZOOM.get(zoom, [0, 1, 2, 3])
        print(f"  ✓ {name}: lat={lat} lon={lon} zoom={zoom} roadTypes={road_types}")

    clear_stale_cache()

    counter = 0  # Reset du compteur global
    errors = 0

    for zone_name, zone_url in ZONES.items():
        try:
            capture_zone(zone_name, zone_url, api_key, now)
        except Exception as e:
            print(f"[{zone_name}] ✗ Erreur: {e}")
            import traceback
            traceback.print_exc()
            errors += 1

    # Résumé
    print(f"\n{'═'*60}")
    print(f"Résumé: {len(ZONES) - errors}/{len(ZONES)} zones OK")
    print(f"Requêtes API: {counter}")

    # Estimation budget
    # Static image: 1/zone/6h = ~12/jour
    # Vector tiles: ~12 tuiles/zone/cycle × 3 zones × 144 cycles = ~5200/jour
    # Incidents: 1/zone/cycle × 3 × 144 = 432/jour (non-tuile)
    cycles_per_day = 144  # toutes les 10 min
    estimated_daily = counter * cycles_per_day
    print(f"Budget journalier estimé: ~{estimated_daily} requêtes "
          f"({estimated_daily * 100 / 50000:.0f}% du quota)")
    print(f"{'═'*60}")

    try:
        rotate_old_days()
    except Exception as e:
        print(f"[rotation] ✗ Erreur: {e}")

    if errors == len(ZONES):
        sys.exit(1)
