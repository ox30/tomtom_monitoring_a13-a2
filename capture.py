"""
TomTom Traffic Capture — Vector Flow Edition
=============================================
Capture automatique du trafic via l'API TomTom, orchestrée par GitHub Actions.

Architecture (3 couches superposées + annotations) :
  1. Carte de base   → API Static Image (une seule requête HTTP)
  2. Traffic Flow     → Vector Flow Tiles (.pbf)
  3. Incidents        → Vector Incident Tiles (.pbf)
  4. Annotations      → Badges de délai sur les incidents (optionnel, par zone)

Configuration : voir config.py (même dossier)
Dépendances  : requests, Pillow (parseur protobuf intégré)
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
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

# ─── Import configuration ────────────────────────────────────────────────────
from config import *

# ─── Protobuf minimal parser ─────────────────────────────────────────────────

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

        if wire_type == 0:
            val, pos = _decode_varint(data, pos)
        elif wire_type == 1:
            val = struct.unpack('<d', data[pos:pos+8])[0]
            pos += 8
        elif wire_type == 2:
            length, pos = _decode_varint(data, pos)
            val = data[pos:pos+length]
            pos += length
        elif wire_type == 5:
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
    """Parse une tuile MVT — retourne les features du layer 'Traffic flow'."""
    if not data or len(data) < 2:
        return []

    tile = _parse_protobuf(data)
    features_out = []

    for layer_data in tile.get(3, []):
        layer = _parse_protobuf(layer_data)
        name = layer.get(1, [b''])[0]
        if isinstance(name, bytes):
            name = name.decode('utf-8', errors='ignore')
        if name != "Traffic flow":
            continue

        extent = layer.get(5, [4096])[0]
        keys = []
        for k in layer.get(3, []):
            keys.append(k.decode('utf-8', errors='ignore') if isinstance(k, bytes) else str(k))

        values = []
        for v_data in layer.get(4, []):
            v_fields = _parse_protobuf(v_data)
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

        for feat_data in layer.get(2, []):
            feat = _parse_protobuf(feat_data)
            tags_raw = _decode_packed_uint32(feat.get(2, [b''])[0]) if 2 in feat and isinstance(feat[2][0], bytes) else []
            props = {}
            for j in range(0, len(tags_raw) - 1, 2):
                k_idx = tags_raw[j]
                v_idx = tags_raw[j + 1]
                if k_idx < len(keys) and v_idx < len(values):
                    props[keys[k_idx]] = values[v_idx]

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


def parse_mvt_tile_incidents(data):
    """Parse une tuile MVT — retourne les features du layer 'Traffic incident flow'."""
    if not data or len(data) < 2:
        return []

    tile = _parse_protobuf(data)
    features_out = []

    for layer_data in tile.get(3, []):
        layer = _parse_protobuf(layer_data)
        name = layer.get(1, [b''])[0]
        if isinstance(name, bytes):
            name = name.decode('utf-8', errors='ignore')
        if name != "Traffic incident flow":
            continue

        extent = layer.get(5, [4096])[0]
        keys = []
        for k in layer.get(3, []):
            keys.append(k.decode('utf-8', errors='ignore') if isinstance(k, bytes) else str(k))

        values = []
        for v_data in layer.get(4, []):
            v_fields = _parse_protobuf(v_data)
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

        for feat_data in layer.get(2, []):
            feat = _parse_protobuf(feat_data)
            tags_raw = _decode_packed_uint32(feat.get(2, [b''])[0]) if 2 in feat and isinstance(feat[2][0], bytes) else []
            props = {}
            for j in range(0, len(tags_raw) - 1, 2):
                k_idx = tags_raw[j]
                v_idx = tags_raw[j + 1]
                if k_idx < len(keys) and v_idx < len(values):
                    props[keys[k_idx]] = values[v_idx]

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


# ─── Traffic category ─────────────────────────────────────────────────────────

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
    """Calcule la grille de tuiles nécessaire pour couvrir le viewport."""
    center_tx, center_ty = lat_lon_to_tile(lat, lon, zoom)
    center_px = center_tx * tile_size
    center_py = center_ty * tile_size
    origin_px = center_px - width / 2
    origin_py = center_py - height / 2

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


# ─── Décalage directionnel ────────────────────────────────────────────────────

def _offset_polyline(coords, offset_px):
    """Décale une polyligne vers la DROITE du sens de circulation."""
    if len(coords) < 2 or abs(offset_px) < 0.5:
        return coords

    result = []
    n = len(coords)

    for i in range(n):
        if i == 0:
            dx = coords[1][0] - coords[0][0]
            dy = coords[1][1] - coords[0][1]
        elif i == n - 1:
            dx = coords[n-1][0] - coords[n-2][0]
            dy = coords[n-1][1] - coords[n-2][1]
        else:
            dx = coords[i+1][0] - coords[i-1][0]
            dy = coords[i+1][1] - coords[i-1][1]

        length = math.hypot(dx, dy)
        if length < 0.01:
            result.append(coords[i])
            continue

        ux = dx / length
        uy = dy / length
        nx = -uy
        ny = ux

        new_x = coords[i][0] + nx * offset_px
        new_y = coords[i][1] + ny * offset_px
        result.append((int(round(new_x)), int(round(new_y))))

    return result


# ─── API calls ────────────────────────────────────────────────────────────────

counter = 0


def api_get(url, binary=False):
    """Requête GET avec gestion d'erreurs."""
    global counter
    counter += 1
    resp = requests.get(url, timeout=30)
    if resp.status_code == 200:
        return resp.content if binary else resp.json()
    print(f"  ⚠ HTTP {resp.status_code} pour {url[:100]}...")
    return None


def _apply_light_style(img):
    """Transforme une image carte en version 'Light' (fond gris clair désaturé)."""
    rgb = img.convert("RGB")
    enhancer = ImageEnhance.Color(rgb)
    rgb = enhancer.enhance(LIGHT_SATURATION)
    enhancer = ImageEnhance.Brightness(rgb)
    rgb = enhancer.enhance(LIGHT_BRIGHTNESS)
    enhancer = ImageEnhance.Contrast(rgb)
    rgb = enhancer.enhance(LIGHT_CONTRAST)
    return rgb.convert("RGBA")


def download_base_image(lat, lon, zoom, width, height, api_key):
    """Télécharge la carte de base via l'API Static Image de TomTom."""
    w = min(width, 8192)
    h = min(height, 8192)
    api_style = "night" if BASE_MAP_STYLE == "night" else "main"

    url = (
        f"https://api.tomtom.com/map/1/staticimage"
        f"?key={api_key}"
        f"&center={lon},{lat}"
        f"&zoom={zoom}"
        f"&width={w}&height={h}"
        f"&format=png"
        f"&layer=basic&style={api_style}"
        f"&language=de-DE"
    )
    print(f"  📍 Base map: {w}×{h} zoom={zoom} style={BASE_MAP_STYLE}")
    data = api_get(url, binary=True)
    if data:
        img = Image.open(BytesIO(data)).convert("RGBA")
        if img.size != (width, height):
            img = img.resize((width, height), Image.LANCZOS)
        if BASE_MAP_STYLE == "light":
            img = _apply_light_style(img)
        return img
    return None


def download_vector_flow(lat, lon, zoom, width, height, api_key, tile_render_size=None):
    """Télécharge et dessine les tuiles vectorielles de trafic flow (.pbf)."""
    if tile_render_size is None:
        tile_render_size = TILE_SIZE

    road_types = ROAD_TYPES_BY_ZOOM.get(zoom, [0, 1, 2, 3])
    road_types_param = "[" + ",".join(str(r) for r in road_types) + "]"

    tiles, origin_px, origin_py = get_tile_grid(
        lat, lon, zoom, width, height, tile_render_size
    )

    flow_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(flow_img)

    n_tiles = len(tiles)
    n_features = 0
    n_downloaded = 0

    for tile_x, tile_y, px_off, py_off in tiles:
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
            road_type = tags.get('road_type', '')
            outline_w, main_w = LINE_WIDTH.get(road_type, DEFAULT_WIDTH)

            for line in feat['geometry']:
                if len(line) < 2:
                    continue

                pixel_line = []
                for tx_coord, ty_coord in line:
                    px = px_off + (tx_coord / extent) * tile_render_size
                    py = py_off + (ty_coord / extent) * tile_render_size
                    pixel_line.append((px, py))

                margin = 50
                if all(x < -margin or x > width + margin or y < -margin or y > height + margin
                       for x, y in pixel_line):
                    continue

                n_features += 1
                coords = [(int(round(x)), int(round(y))) for x, y in pixel_line]

                if len(coords) >= 2:
                    offset_px = max(1, outline_w * FLOW_OFFSET)
                    shifted = _offset_polyline(coords, offset_px)
                    vis_outline = max(1, int(math.ceil(outline_w * FLOW_VIS_OUTLINE)))
                    vis_main = max(1, int(math.ceil(main_w * FLOW_VIS_MAIN)))
                    draw.line(shifted, fill=outline_color, width=vis_outline, joint="curve")
                    draw.line(shifted, fill=main_color, width=vis_main, joint="curve")

    print(f"  🚗 Flow: {n_downloaded}/{n_tiles} tuiles, {n_features} segments"
          f" (roadTypes={road_types})")
    return flow_img


def download_incidents(lat, lon, zoom, width, height, api_key,
                       tile_render_size=None, collect_annotations=False):
    """
    Télécharge les incidents via Vector Incident Tiles (.pbf) et les dessine.

    Si collect_annotations=True, retourne (image, annotations_list) où
    annotations_list contient les données nécessaires pour dessiner les badges.
    Sinon, retourne (image, []).
    """
    if tile_render_size is None:
        tile_render_size = TILE_SIZE

    tiles, origin_px, origin_py = get_tile_grid(
        lat, lon, zoom, width, height, tile_render_size
    )

    inc_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(inc_img)

    n_tiles = len(tiles)
    n_downloaded = 0

    # Phase 1 : Collecter tous les incidents
    all_incidents = []

    for tile_x, tile_y, px_off, py_off in tiles:
        max_tile = 2 ** zoom - 1
        if tile_y < 0 or tile_y > max_tile:
            continue
        tx = tile_x % (max_tile + 1)

        url = (
            f"https://api.tomtom.com/traffic/map/4/tile/incidents"
            f"/{zoom}/{tx}/{tile_y}.pbf"
            f"?key={api_key}"
            f"&tags=[icon_category,magnitude,road_type,delay,id]"
        )

        data = api_get(url, binary=True)
        if not data:
            continue
        n_downloaded += 1

        features = parse_mvt_tile_incidents(data)

        for feat in features:
            tags = feat['tags']
            extent = feat.get('extent', 4096)

            icon_cat = None
            for key, val in tags.items():
                if key == 'icon_category' or key.startswith('icon_category_'):
                    try:
                        icon_cat = int(val)
                    except (ValueError, TypeError):
                        pass
                    if icon_cat is not None:
                        break

            if icon_cat is None:
                continue

            style = INCIDENT_STYLE.get(icon_cat)
            if style is None:
                continue

            priority = INCIDENT_PRIORITY.get(icon_cat, 0)
            road_type = tags.get('road_type', '')
            outline_w, main_w = INCIDENT_WIDTH.get(road_type, INCIDENT_DEFAULT_WIDTH)

            magnitude = 0
            if 'magnitude' in tags:
                try:
                    magnitude = int(tags['magnitude'])
                except (ValueError, TypeError):
                    magnitude = 0

            # Récupérer le delay (en secondes)
            delay = 0
            if 'delay' in tags:
                try:
                    delay = int(float(tags['delay']))
                except (ValueError, TypeError):
                    delay = 0

            # Récupérer l'identifiant unique de l'incident
            # Permet de dédupliquer les segments du même incident sur plusieurs tuiles
            incident_id = tags.get('id', None)

            for line in feat['geometry']:
                if len(line) < 2:
                    continue

                pixel_line = []
                for tx_coord, ty_coord in line:
                    px = px_off + (tx_coord / extent) * tile_render_size
                    py = py_off + (ty_coord / extent) * tile_render_size
                    pixel_line.append((px, py))

                margin = 50
                if all(x < -margin or x > width + margin or
                       y < -margin or y > height + margin
                       for x, y in pixel_line):
                    continue

                coords = [(int(round(x)), int(round(y))) for x, y in pixel_line]
                if len(coords) < 2:
                    continue

                all_incidents.append((priority, style, icon_cat, magnitude,
                                      outline_w, main_w, coords, delay, incident_id))

    # Phase 2 : Trier par priorité
    all_incidents.sort(key=lambda x: x[0])

    # Phase 3 : Dessiner + collecter les annotations
    n_hatched_red = 0
    n_hatched_grey = 0
    n_solid = 0
    annotations = []
    annotated_ids = set()  # IDs d'incidents déjà annotés (évite les doublons)

    for priority, style, icon_cat, magnitude, outline_w, main_w, coords, delay, incident_id in all_incidents:
        offset_px = max(1, outline_w * INCIDENT_OFFSET)
        shifted = _offset_polyline(coords, offset_px)
        vis_outline = max(1, int(math.ceil(outline_w * INCIDENT_VIS_OUTLINE)))
        vis_main = max(1, int(math.ceil(main_w * INCIDENT_VIS_MAIN)))

        if style == "hatched_red":
            _draw_hatched_tube(draw, shifted, HATCHED_RED_COLORS, vis_outline, vis_main)
            n_hatched_red += 1
        elif style == "hatched_grey":
            _draw_hatched_tube(draw, shifted, HATCHED_GREY_COLORS, vis_outline, vis_main)
            n_hatched_grey += 1
        elif style == "solid":
            colors = INCIDENT_MAGNITUDE_COLORS.get(magnitude,
                     INCIDENT_MAGNITUDE_COLORS[0])
            outline_c, main_c = colors
            draw.line(shifted, fill=outline_c, width=vis_outline, joint="curve")
            draw.line(shifted, fill=main_c, width=vis_main, joint="curve")
            n_solid += 1

        # Collecter les données d'annotation si activé
        if collect_annotations and INCIDENT_ANNOTATIONS.get(icon_cat, False):
            if delay >= ANNOTATION_MIN_DELAY_SEC:
                # Dédupliquer : un seul badge par incident unique
                # (un même incident peut avoir plusieurs segments sur différentes tuiles)
                if incident_id is not None and incident_id in annotated_ids:
                    continue  # Ce segment appartient à un incident déjà annoté
                if incident_id is not None:
                    annotated_ids.add(incident_id)

                # Point d'ancrage = DÉBUT de la polyligne (arrière du bouchon)
                anchor_x, anchor_y = shifted[0][0], shifted[0][1]

                # Direction du trafic au point d'ancrage
                if len(shifted) >= 2:
                    dx = shifted[1][0] - shifted[0][0]
                    dy = shifted[1][1] - shifted[0][1]
                    d_len = math.hypot(dx, dy)
                    if d_len > 0.01:
                        dir_x, dir_y = dx / d_len, dy / d_len
                    else:
                        dir_x, dir_y = 1.0, 0.0
                else:
                    dir_x, dir_y = 1.0, 0.0

                annotations.append({
                    "anchor_x": anchor_x,
                    "anchor_y": anchor_y,
                    "dir_x": dir_x,
                    "dir_y": dir_y,
                    "delay": delay,
                    "magnitude": magnitude,
                    "icon_cat": icon_cat,
                    "priority": priority,
                })

    total = n_hatched_red + n_hatched_grey + n_solid
    print(f"  ⚠ Incidents: {total} dessinés "
          f"({n_hatched_red} fermé, {n_hatched_grey} travaux/météo, {n_solid} bouchons)"
          f" — {n_downloaded}/{n_tiles} tuiles")

    if collect_annotations and annotations:
        print(f"  🏷 Annotations: {len(annotations)} incidents à annoter")

    return inc_img, annotations


def _polyline_midpoint(coords):
    """Calcule le point milieu d'une polyligne (par longueur cumulée)."""
    if len(coords) < 2:
        return coords[0] if coords else (0, 0)

    # Calculer la longueur totale
    total_len = 0.0
    seg_lengths = []
    for i in range(len(coords) - 1):
        d = math.hypot(coords[i+1][0] - coords[i][0],
                       coords[i+1][1] - coords[i][1])
        seg_lengths.append(d)
        total_len += d

    if total_len < 1:
        return coords[0]

    # Trouver le point à mi-chemin
    target = total_len / 2.0
    accumulated = 0.0
    for i, seg_len in enumerate(seg_lengths):
        if accumulated + seg_len >= target:
            # Interpoler sur ce segment
            remaining = target - accumulated
            t = remaining / seg_len if seg_len > 0 else 0
            x = coords[i][0] + t * (coords[i+1][0] - coords[i][0])
            y = coords[i][1] + t * (coords[i+1][1] - coords[i][1])
            return (x, y)
        accumulated += seg_len

    # Fallback
    return coords[-1]


def _draw_hatched_tube(draw, coords, colors, outline_w, main_w):
    """Dessine un tube hachuré TomTom le long d'une polyligne."""
    color, grey_fill = colors
    if len(coords) < 2:
        return

    draw.line(coords, fill=color, width=outline_w, joint="curve")
    draw.line(coords, fill=grey_fill, width=main_w, joint="curve")

    square_size = max(2, main_w)
    unit_size = square_size * 3.0
    half_sq = square_size / 2.0
    half_w = main_w / 2.0

    seg_idx = 0
    seg_consumed = half_sq

    while seg_idx < len(coords) - 1:
        x0, y0 = coords[seg_idx]
        x1, y1 = coords[seg_idx + 1]
        seg_len = math.hypot(x1 - x0, y1 - y0)

        if seg_len < 0.5:
            seg_idx += 1
            seg_consumed = 0.0
            continue

        ux, uy = (x1 - x0) / seg_len, (y1 - y0) / seg_len
        nx, ny = -uy, ux

        while seg_consumed < seg_len:
            cx = x0 + ux * seg_consumed
            cy = y0 + uy * seg_consumed

            corners = [
                (cx - ux * half_sq + nx * half_w, cy - uy * half_sq + ny * half_w),
                (cx + ux * half_sq + nx * half_w, cy + uy * half_sq + ny * half_w),
                (cx + ux * half_sq - nx * half_w, cy + uy * half_sq - ny * half_w),
                (cx - ux * half_sq - nx * half_w, cy - uy * half_sq - ny * half_w),
            ]
            corners_int = [(int(round(px)), int(round(py))) for px, py in corners]
            draw.polygon(corners_int, fill=color)

            seg_consumed += unit_size

        seg_consumed -= seg_len
        seg_idx += 1


# ─── Annotations — Badges de délai ───────────────────────────────────────────

def _get_annotation_font():
    """Charge la police pour les annotations."""
    # Essayer DejaVu Sans Bold (disponible sur Ubuntu / GitHub Actions)
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, ANNOTATION_FONT_SIZE)
        except (OSError, IOError):
            continue
    # Fallback — police par défaut de Pillow (moins jolie mais fonctionnelle)
    try:
        return ImageFont.load_default(size=ANNOTATION_FONT_SIZE)
    except TypeError:
        return ImageFont.load_default()


def _format_delay(seconds):
    """Formate un délai en texte lisible pour le badge."""
    minutes = seconds // 60
    if minutes < 1:
        return None  # Trop court, pas d'annotation
    if minutes < 60:
        return f"+{minutes}min"
    hours = minutes // 60
    remaining_min = minutes % 60
    if remaining_min == 0:
        return f"+{hours}h"
    return f"+{hours}h{remaining_min:02d}"


def _draw_annotations(image, annotations, supersample_factor):
    """
    Dessine les badges de délai sur l'image finale (après downscale).
    Chaque badge est :
      - positionné à l'ARRIÈRE de l'incident (début de la géométrie)
      - décalé vers la DROITE du sens de circulation
      - relié au point d'ancrage par un petit cône (bulle de dialogue)

    annotations : liste de dicts avec anchor_x, anchor_y, dir_x, dir_y
                  (en pixels supersamplés), delay, magnitude, icon_cat, priority
    supersample_factor : facteur de supersampling (pour convertir les coords)
    """
    if not annotations:
        return

    font = _get_annotation_font()
    draw = ImageDraw.Draw(image)
    pad_x, pad_y = ANNOTATION_PADDING

    # Trier par priorité descendante + delay (les plus importants placés en premier)
    annotations.sort(key=lambda a: (-a["priority"], -a["delay"]))

    # Liste des rectangles occupés pour l'anti-chevauchement
    occupied = []

    n_drawn = 0
    n_skipped = 0

    for ann in annotations:
        # Convertir les coordonnées supersamplées → viewport
        anchor_x = ann["anchor_x"] / supersample_factor
        anchor_y = ann["anchor_y"] / supersample_factor
        dir_x = ann["dir_x"]
        dir_y = ann["dir_y"]

        # Vérifier que l'ancrage est dans le viewport
        if anchor_x < 0 or anchor_x > VIEWPORT_WIDTH or \
           anchor_y < 0 or anchor_y > VIEWPORT_HEIGHT:
            continue

        # Formater le texte
        text = _format_delay(ann["delay"])
        if text is None:
            continue

        # Mesurer le texte
        bbox = font.getbbox(text)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        badge_w = text_w + 2 * pad_x
        badge_h = text_h + 2 * pad_y

        # Perpendiculaire DROITE du sens de circulation (screen coords, Y vers le bas)
        # Droite = (-dir_y, dir_x)
        perp_x = -dir_y
        perp_y = dir_x

        # Position du centre du badge = ancrage + perpendiculaire × distance
        # La distance est calculée jusqu'au BORD du badge (pas son centre),
        # pour que le cône soit toujours visible entre la route et le badge.
        # On ajoute la demi-dimension du badge qui fait face à l'ancrage.
        edge_offset = abs(perp_x) * (badge_w / 2) + abs(perp_y) * (badge_h / 2)
        total_offset = ANNOTATION_OFFSET_DISTANCE + edge_offset
        badge_cx = anchor_x + perp_x * total_offset
        badge_cy = anchor_y + perp_y * total_offset

        # Rectangle du badge
        badge_x1 = int(badge_cx - badge_w / 2)
        badge_y1 = int(badge_cy - badge_h / 2)
        badge_x2 = badge_x1 + badge_w
        badge_y2 = badge_y1 + badge_h

        # Garder le badge dans le viewport (avec marge)
        margin = 3
        if badge_x1 < margin:
            shift = margin - badge_x1
            badge_x1 += shift
            badge_x2 += shift
            badge_cx += shift
        if badge_y1 < margin:
            shift = margin - badge_y1
            badge_y1 += shift
            badge_y2 += shift
            badge_cy += shift
        if badge_x2 > VIEWPORT_WIDTH - margin:
            shift = badge_x2 - (VIEWPORT_WIDTH - margin)
            badge_x1 -= shift
            badge_x2 -= shift
            badge_cx -= shift
        if badge_y2 > VIEWPORT_HEIGHT - margin:
            shift = badge_y2 - (VIEWPORT_HEIGHT - margin)
            badge_y1 -= shift
            badge_y2 -= shift
            badge_cy -= shift

        # Anti-chevauchement : vérifier la distance avec les badges existants
        too_close = False
        for (ox1, oy1, ox2, oy2) in occupied:
            other_cx = (ox1 + ox2) / 2
            other_cy = (oy1 + oy2) / 2
            dist = math.hypot(badge_cx - other_cx, badge_cy - other_cy)
            if dist < ANNOTATION_MIN_DISTANCE:
                too_close = True
                break

        if too_close:
            n_skipped += 1
            continue

        # Choisir la couleur de fond
        if ann["icon_cat"] == 6:  # Jam → couleur par magnitude
            bg_color = ANNOTATION_BADGE_COLORS.get(
                ann["magnitude"], ANNOTATION_BADGE_COLORS[0]
            )
        else:
            bg_color = ANNOTATION_DEFAULT_BADGE_COLOR

        # ─── Dessiner le cône AVANT le badge (bulle de dialogue) ──────
        # Le badge sera dessiné par-dessus, couvrant proprement la base du cône.
        tip_x = int(round(anchor_x))
        tip_y = int(round(anchor_y))

        # Déterminer quel bord du badge fait face à l'ancrage
        edge_centers = {
            "top":    (badge_cx, badge_y1),
            "bottom": (badge_cx, badge_y2),
            "left":   (badge_x1, badge_cy),
            "right":  (badge_x2, badge_cy),
        }
        closest_edge = min(edge_centers, key=lambda e: math.hypot(
            edge_centers[e][0] - anchor_x, edge_centers[e][1] - anchor_y
        ))
        edge_x, edge_y = edge_centers[closest_edge]

        # Base du cône sur le bord le plus proche
        half_cone = ANNOTATION_CONE_WIDTH / 2
        if closest_edge in ("top", "bottom"):
            base1 = (int(edge_x - half_cone), int(edge_y))
            base2 = (int(edge_x + half_cone), int(edge_y))
        else:
            base1 = (int(edge_x), int(edge_y - half_cone))
            base2 = (int(edge_x), int(edge_y + half_cone))

        # Dessiner le cône : fond couleur badge + contour sombre
        cone_pts = [(tip_x, tip_y), base1, base2]
        draw.polygon(cone_pts, fill=bg_color, outline=ANNOTATION_BORDER_COLOR)

        # ─── Dessiner le badge PAR-DESSUS le cône ─────────────────────
        # 1. Bordure
        draw.rounded_rectangle(
            [badge_x1 - ANNOTATION_BORDER_WIDTH,
             badge_y1 - ANNOTATION_BORDER_WIDTH,
             badge_x2 + ANNOTATION_BORDER_WIDTH,
             badge_y2 + ANNOTATION_BORDER_WIDTH],
            radius=ANNOTATION_CORNER_RADIUS + ANNOTATION_BORDER_WIDTH,
            fill=ANNOTATION_BORDER_COLOR,
        )
        # 2. Fond coloré
        draw.rounded_rectangle(
            [badge_x1, badge_y1, badge_x2, badge_y2],
            radius=ANNOTATION_CORNER_RADIUS,
            fill=bg_color,
        )
        # 3. Texte centré
        text_x = badge_x1 + pad_x - bbox[0]
        text_y = badge_y1 + pad_y - bbox[1]
        draw.text((text_x, text_y), text, fill=ANNOTATION_TEXT_COLOR, font=font)

        occupied.append((badge_x1, badge_y1, badge_x2, badge_y2))
        n_drawn += 1

    if n_drawn > 0 or n_skipped > 0:
        print(f"  🏷 Annotations dessinées: {n_drawn} badges"
              f" ({n_skipped} masqués pour chevauchement)")


# ─── Capture d'une zone ──────────────────────────────────────────────────────

def capture_zone(zone_name, zone_config, api_key, now):
    """Capture complète d'une zone : base + flow + incidents + annotations."""
    zone_url = zone_config["url"]
    want_annotations = zone_config.get("annotations", False)

    lat, lon, zoom = parse_zone_url(zone_url)
    print(f"\n{'─'*60}")
    print(f"[{zone_name}] lat={lat} lon={lon} zoom={zoom}"
          f" annotations={'ON' if want_annotations else 'OFF'}")

    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M")

    # Dimensions de rendu interne (supersampling)
    render_w = VIEWPORT_WIDTH  * SUPERSAMPLE
    render_h = VIEWPORT_HEIGHT * SUPERSAMPLE
    tile_render_size = TILE_SIZE * SUPERSAMPLE

    if SUPERSAMPLE > 1:
        print(f"  🔍 Supersampling ×{SUPERSAMPLE} — rendu {render_w}×{render_h}"
              f" → downscale {VIEWPORT_WIDTH}×{VIEWPORT_HEIGHT}")

    # 1. Carte de base (toujours à VIEWPORT size, puis upscale en RAM)
    cache_path = CACHE_DIR / f"{zone_name}_z{zoom}.png"
    if cache_path.exists():
        print(f"  📦 Base map: cache OK")
        base_img = Image.open(cache_path).convert("RGBA")
    else:
        base_img = download_base_image(lat, lon, zoom, VIEWPORT_WIDTH, VIEWPORT_HEIGHT, api_key)
        if base_img is None:
            print(f"  ✗ Impossible de télécharger la carte de base")
            return 0
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        base_img.save(str(cache_path), "PNG")
        print(f"  💾 Base map cachée localement")

    if SUPERSAMPLE > 1:
        base_img = base_img.resize((render_w, render_h), Image.LANCZOS)

    # 2. Traffic Flow
    flow_img = download_vector_flow(
        lat, lon, zoom, render_w, render_h, api_key,
        tile_render_size=tile_render_size
    )

    # 3. Incidents (+ collecte des annotations si activé)
    annotations = []
    if INCIDENTS_ENABLED:
        inc_img, annotations = download_incidents(
            lat, lon, zoom, render_w, render_h, api_key,
            tile_render_size=tile_render_size,
            collect_annotations=want_annotations,
        )
    else:
        inc_img = Image.new("RGBA", (render_w, render_h), (0, 0, 0, 0))
        print(f"  ⚠ Incidents désactivés (INCIDENTS_ENABLED=false)")

    # 4. Composer l'image finale (en haute résolution)
    composite = base_img.copy()
    composite = Image.alpha_composite(composite, flow_img)
    composite = Image.alpha_composite(composite, inc_img)

    # 5. Downscale LANCZOS → résolution finale
    if SUPERSAMPLE > 1:
        composite = composite.resize(
            (VIEWPORT_WIDTH, VIEWPORT_HEIGHT), Image.LANCZOS
        )

    # 6. Annotations — dessinées APRÈS le downscale pour un texte net
    if want_annotations and annotations:
        _draw_annotations(composite, annotations, SUPERSAMPLE)

    # 7. Sauvegarder en JPEG
    zone_dir = OUTPUT_DIR / date_str / zone_name
    zone_dir.mkdir(parents=True, exist_ok=True)
    out_path = zone_dir / f"{date_str}-{time_str}_{zone_name}.jpg"
    composite.convert("RGB").save(str(out_path), "JPEG", quality=OUTPUT_QUALITY)
    size_kb = out_path.stat().st_size / 1024
    print(f"  ✅ {out_path} ({size_kb:.0f} KB) — JPEG q={OUTPUT_QUALITY}")

    return counter


def save_bases(api_key):
    """Télécharge et archive les cartes de base — appelée 1× par run."""
    now = datetime.now(TIMEZONE)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M")

    print(f"\n{'='*60}")
    print(f"SAUVEGARDE CARTES DE BASE — {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"{'='*60}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    for zone_name, zone_config in ZONES.items():
        zone_url = zone_config["url"]
        lat, lon, zoom = parse_zone_url(zone_url)
        print(f"\n[{zone_name}] zoom={zoom}")

        base_img = download_base_image(lat, lon, zoom, VIEWPORT_WIDTH, VIEWPORT_HEIGHT, api_key)
        if base_img is None:
            print(f"  ✗ Échec téléchargement base")
            continue

        cache_path = CACHE_DIR / f"{zone_name}_z{zoom}.png"
        base_img.save(str(cache_path), "PNG")
        print(f"  💾 Cache local: {cache_path}")

        base_dir = BASES_DIR / date_str / zone_name
        base_dir.mkdir(parents=True, exist_ok=True)
        base_path = base_dir / f"{date_str}-{time_str}_{zone_name}_base.jpg"
        base_img.convert("RGB").save(str(base_path), "JPEG", quality=OUTPUT_QUALITY)
        size_kb = base_path.stat().st_size / 1024
        print(f"  📁 Archivé: {base_path} ({size_kb:.0f} KB)")


def rotate_old_days():
    """Supprime les captures et bases de plus de RETENTION_DAYS jours."""
    cutoff = datetime.now(TIMEZONE).date() - timedelta(days=RETENTION_DAYS)
    for root_dir in [OUTPUT_DIR, BASES_DIR]:
        if not root_dir.exists():
            continue
        for day_dir in root_dir.iterdir():
            if day_dir.is_dir() and day_dir.name.startswith("20"):
                try:
                    folder_date = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
                    if folder_date < cutoff:
                        import shutil
                        shutil.rmtree(day_dir)
                        print(f"  🗑 Supprimé: {root_dir.name}/{day_dir.name}")
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


# ─── Budget API ───────────────────────────────────────────────────────────────

def _count_tiles_for_zone(lat, lon, zoom):
    """Compte le nombre de tuiles nécessaires pour couvrir le viewport."""
    tiles, _, _ = get_tile_grid(lat, lon, zoom, VIEWPORT_WIDTH, VIEWPORT_HEIGHT, TILE_SIZE)
    return len(tiles)


def print_budget_report():
    """Affiche le rapport de consommation API estimée."""
    print(f"\n{'─'*65}")
    print("📊 BUDGET API — Estimation de consommation")
    print(f"{'─'*65}")

    total_per_cycle = 0
    total_base_per_run = 0

    for name, zone_config in ZONES.items():
        lat, lon, zoom = parse_zone_url(zone_config["url"])
        n_tiles = _count_tiles_for_zone(lat, lon, zoom)
        road_types = ROAD_TYPES_BY_ZOOM.get(zoom, [0, 1, 2, 3])

        flow = n_tiles
        inc = n_tiles
        base = 1
        per_cycle = flow + inc

        ann_tag = " 🏷" if zone_config.get("annotations", False) else ""
        print(f"  {name} (zoom={zoom}, roadTypes={road_types}){ann_tag}")
        print(f"    {n_tiles} tuiles × 2 couches = {per_cycle}/cycle  |  base: {base}/run")

        total_per_cycle += per_cycle
        total_base_per_run += base

    run_cost = total_per_cycle * CYCLES_PER_RUN + total_base_per_run
    daily_cost = total_per_cycle * CYCLES_PER_RUN * RUNS_PER_DAY + total_base_per_run * RUNS_PER_DAY
    pct = daily_cost * 100 / DAILY_QUOTA
    remaining = DAILY_QUOTA - daily_cost

    if total_per_cycle > 0:
        avg_per_zone = total_per_cycle // len(ZONES)
        extra_zones = remaining // (avg_per_zone * CYCLES_PER_RUN * RUNS_PER_DAY) if avg_per_zone > 0 else 0
    else:
        extra_zones = 0

    print(f"\n  {'─'*55}")
    print(f"  Par cycle:       {total_per_cycle} requêtes ({len(ZONES)} zones)")
    print(f"  Par run (5h45):  {run_cost} requêtes ({CYCLES_PER_RUN} cycles + {total_base_per_run} bases)")
    print(f"  Par jour (×{RUNS_PER_DAY}):   {daily_cost} / {DAILY_QUOTA} = {pct:.1f}%")
    print(f"  Marge:           {remaining} req/jour ≈ {extra_zones} zones supplémentaires")
    print(f"{'─'*65}")

    if SUPERSAMPLE > 1:
        print(f"  🔍 Supersampling ×{SUPERSAMPLE} actif — qualité améliorée, zéro surcoût API")
    if pct > 90:
        print("  ⚠ ATTENTION : consommation proche du quota !")
    elif pct > 70:
        print("  ⚠ Consommation élevée — prudence avant d'ajouter des zones")
    else:
        print("  ✓ Budget confortable")
    print()


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TomTom Traffic Capture — Vector Flow")
    parser.add_argument("--save-bases", action="store_true",
                        help="Télécharger et archiver les cartes de base (1× par run)")
    args = parser.parse_args()

    api_key = os.environ.get("TOMTOM_API_KEY", "")
    if not api_key:
        print("✗ TOMTOM_API_KEY non définie")
        print("  → export TOMTOM_API_KEY='votre_clé'")
        print("  → ou Settings > Secrets > TOMTOM_API_KEY dans GitHub")
        sys.exit(1)

    now = datetime.now(TIMEZONE)
    print(f"═══ TomTom Vector Flow Capture — {now.strftime('%Y-%m-%d %H:%M %Z')} ═══")

    # Afficher les zones configurées
    print("\nZones configurées:")
    for name, zone_config in ZONES.items():
        lat, lon, zoom = parse_zone_url(zone_config["url"])
        road_types = ROAD_TYPES_BY_ZOOM.get(zoom, [0, 1, 2, 3])
        ann = "✓" if zone_config.get("annotations", False) else "X"
        print(f"  ✓ {name}: zoom={zoom} roadTypes={road_types} annotations={ann}")

    print_budget_report()
    clear_stale_cache()

    if args.save_bases:
        save_bases(api_key)
    else:
        counter = 0
        errors = 0

        for zone_name, zone_config in ZONES.items():
            try:
                capture_zone(zone_name, zone_config, api_key, now)
            except Exception as e:
                print(f"[{zone_name}] ✗ Erreur: {e}")
                import traceback
                traceback.print_exc()
                errors += 1

        print(f"\n{'═'*60}")
        print(f"Résumé: {len(ZONES) - errors}/{len(ZONES)} zones OK")
        print(f"Requêtes API: {counter}")
        print(f"{'═'*60}")

        if errors == len(ZONES):
            sys.exit(1)

    try:
        rotate_old_days()
    except Exception as e:
        print(f"[rotation] ✗ Erreur: {e}")
