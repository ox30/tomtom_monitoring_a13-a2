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

# Export structuré des incidents (v14 préparatoire) — génère incidents_{date}.json
# en parallèle des images. Import optionnel : si le module est absent, le système
# continue à produire les images normalement.
try:
    import incidents_export as inc_export
    INCIDENTS_EXPORT_ENABLED = True
except ImportError:
    inc_export = None
    INCIDENTS_EXPORT_ENABLED = False

# Notifications ntfy — import optionnel. Si config_ntfy.py absent ou mal formé,
# le système continue sans notifs (les events restent loggés dans le JSON).
try:
    from config_ntfy import NTFY_NOTIFICATIONS, NTFY_EPISODE_OPEN_THRESHOLD
except ImportError:
    NTFY_NOTIFICATIONS = {}
    NTFY_EPISODE_OPEN_THRESHOLD = 3

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


def api_get(url, binary=False, zone_name=None, api_layer=None):
    """Requête GET avec gestion d'erreurs.

    zone_name, api_layer : utilisés uniquement pour le journal d'événements
    (contexte lors de timeout ou d'HTTP error). Pas d'impact fonctionnel.
    """
    global counter
    counter += 1
    try:
        resp = requests.get(url, timeout=30)
    except requests.exceptions.Timeout:
        print(f"  ⚠ Timeout pour {url[:100]}...")
        _log_event(
            event_type="api_timeout",
            zone=zone_name,
            severity="warning",
            details={
                "api_layer": api_layer,
                "url_endpoint": url.split("?")[0][:200],
                "timeout_seconds": 30,
            },
        )
        return None
    except requests.exceptions.RequestException as e:
        print(f"  ⚠ Erreur requête : {type(e).__name__}")
        _log_event(
            event_type="api_request_error",
            zone=zone_name,
            severity="warning",
            details={
                "api_layer": api_layer,
                "url_endpoint": url.split("?")[0][:200],
                "error_type": type(e).__name__,
                "error_message": str(e)[:200],
            },
        )
        return None

    if resp.status_code == 200:
        return resp.content if binary else resp.json()

    print(f"  ⚠ HTTP {resp.status_code} pour {url[:100]}...")
    _log_event(
        event_type="api_http_error",
        zone=zone_name,
        severity="error" if resp.status_code >= 500 else "warning",
        details={
            "api_layer": api_layer,
            "status_code": resp.status_code,
            "url_endpoint": url.split("?")[0][:200],
        },
    )
    return None


# ─── Journal d'événements cycle-par-cycle ────────────────────────────────────
#
# Écrit un capture_errors.json dans captures/YYYY-MM-DD/ qui accompagne les
# JPG du jour. Le fichier suit le même cycle de vie que les captures :
#   - Poussé sur GitHub (branche captures) à chaque cycle
#   - Embarqué dans le ZIP Proton par Archive.yml (zip -r)
#   - Supprimé par rotate_old_days() après RETENTION_DAYS
#
# Types d'événements supportés :
#   - api_timeout           : timeout requête TomTom
#   - api_http_error        : HTTP 4xx/5xx
#   - api_request_error     : autre erreur réseau (DNS, SSL, etc.)
#   - partial_capture       : n_downloaded < n_tiles pour une couche
#   - data_freeze           : ouverture d'épisode de gel (confirmé)
#   - data_freeze_recovered : clôture d'épisode de gel
#   - zone_capture_failed   : exception dans capture_zone
#
# Les variables _current_date_str et _current_local_time sont settées par
# __main__ (et save_bases) avant tout appel à _log_event(). Si None, l'event
# est silencieusement ignoré pour ne jamais faire planter la capture.

_current_date_str = None      # "YYYY-MM-DD" en timezone locale (Zurich)
_current_local_time = None    # "HH:MM" en timezone locale


def _now_utc_iso():
    """Horodatage UTC ISO sans microsecondes, suffixe Z (compat archive_log.json)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_log_path(date_str):
    """Chemin du log d'erreurs pour une date donnée."""
    return OUTPUT_DIR / date_str / "capture_errors.json"


def _load_error_log(date_str):
    """Charge le log existant ou retourne un squelette vierge."""
    path = _error_log_path(date_str)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "version": "1.0",
        "date": date_str,
        "last_updated_utc": _now_utc_iso(),
        "events": [],
    }


def _save_error_log(date_str, log):
    """Persiste le log. Silencieux en cas d'échec disque."""
    path = _error_log_path(date_str)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        log["last_updated_utc"] = _now_utc_iso()
        path.write_text(
            json.dumps(log, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"  ⚠ Échec écriture {path.name} : {e}")


def _log_event(event_type, zone=None, details=None, severity="warning"):
    """Ajoute un événement au log du jour.

    Écriture directe (read-modify-write à chaque appel) pour que les événements
    soient persistés même si le processus plante ensuite. Coût négligeable
    (fichier JSON de quelques KB, < 10 ms par écriture).

    L'événement est également bufferisé en mémoire pour le dispatch ntfy de fin
    de cycle (_process_cycle_notifications).

    Si _current_date_str n'est pas set (situation anormale), l'event est
    silencieusement ignoré pour ne pas propager une exception vers le code
    de capture.
    """
    if _current_date_str is None:
        return

    event = {
        "timestamp_utc": _now_utc_iso(),
        "local_time": _current_local_time,
        "zone": zone,
        "type": event_type,
        "severity": severity,
        "details": details or {},
    }

    # Buffer mémoire pour le dispatch ntfy en fin de cycle
    _current_cycle_events_buffer.append(event)

    # Persistance directe dans le log JSON du jour
    try:
        log = _load_error_log(_current_date_str)
        log["events"].append(event)
        _save_error_log(_current_date_str, log)
    except Exception as e:
        # Pas question qu'un souci de log fasse planter une capture
        print(f"  ⚠ Échec log événement ({event_type}) : {e}")


# Buffer des événements du cycle courant (réinitialisé à chaque import Python)
# Utilisé par _process_cycle_notifications() en fin de cycle
_current_cycle_events_buffer = []


# ─── Détection de fraîcheur des données API ──────────────────────────────────
#
# Chaque cycle du workflow GitHub Actions lance un nouveau processus Python,
# donc les variables module-level sont réinitialisées toutes les 10 min.
# Pour détecter un gel entre cycles, on persiste l'état dans un JSON sur /tmp
# (qui survit entre commandes bash d'un même run GitHub Actions).
#
# Limite connue : au démarrage d'un nouveau run GitHub Actions (runner frais,
# /tmp vide), le tout premier cycle ne peut pas comparer → pas de détection
# sur ce cycle. À partir du 2e cycle, le check fonctionne normalement.
#
# La règle ≥ 50% des zones figées pour déclencher un événement "data_freeze"
# évite les faux positifs nocturnes (ex : zone_Amsteg-Göschenen sans trafic
# peut être byte-identique plusieurs cycles d'affilée sans qu'il y ait gel
# côté TomTom).

_FRESHNESS_STATE_FILE = Path(
    os.environ.get("RUNNER_TEMP", "/tmp")
) / "capture_freshness_state.json"

_GLOBAL_GEL_THRESHOLD = 0.5   # Fraction de zones figées pour déclarer un gel global


def _load_freshness_state():
    """Charge l'état depuis le disque, ou renvoie un état vierge si absent/corrompu."""
    default = {
        "hashes": {},
        "consecutive_frozen": {},
        "freeze_event_open": {},
        "first_frozen_at_utc": {},
    }
    if not _FRESHNESS_STATE_FILE.exists():
        return default
    try:
        loaded = json.loads(_FRESHNESS_STATE_FILE.read_text())
        # Compat : remplir les clés manquantes si on charge un vieux state
        for k, v in default.items():
            loaded.setdefault(k, v)
        return loaded
    except (json.JSONDecodeError, OSError):
        return default


def _save_freshness_state():
    """Persiste l'état complet pour le prochain cycle. Silencieux si échec disque.

    Préserve les épisodes ntfy si présents dans le fichier existant — ils sont
    mis à jour par _process_cycle_notifications mais peuvent avoir été écrits
    précédemment et ne doivent pas être effacés par un _save_freshness_state
    intermédiaire (appelé depuis _check_data_freshness ou _process_cycle_events).
    """
    # Conserver episode_states si déjà présent dans le fichier
    existing = {}
    if _FRESHNESS_STATE_FILE.exists():
        try:
            existing = json.loads(_FRESHNESS_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}

    try:
        _FRESHNESS_STATE_FILE.write_text(json.dumps({
            "hashes": _last_flow_hash,
            "consecutive_frozen": _consecutive_frozen,
            "freeze_event_open": _freeze_event_open,
            "first_frozen_at_utc": _first_frozen_at,
            "episode_states": existing.get("episode_states", {}),
        }))
    except OSError:
        pass


# Chargement une seule fois à l'import du module
_freshness_state = _load_freshness_state()
_last_flow_hash = _freshness_state["hashes"]              # zone_name -> hash MD5
_consecutive_frozen = _freshness_state["consecutive_frozen"]  # zone_name -> count
_freeze_event_open = _freshness_state["freeze_event_open"]    # zone_name -> bool
_first_frozen_at = _freshness_state["first_frozen_at_utc"]    # zone_name -> ISO|None


def _check_data_freshness(zone_name, flow_bytes_list):
    """
    Détecte un gel de l'API TomTom en comparant les tuiles flow
    au cycle précédent pour cette zone.

    Un gel se manifeste quand TomTom sert des .pbf byte-for-byte identiques
    pendant plusieurs cycles — signe que leur pipeline d'ingestion ou leur CDN
    ne rafraîchit plus les tuiles. Le code continue normalement ; seul un
    warning stdout est loggé ici pour alerter l'opérateur en direct.

    Les événements structurés (data_freeze / data_freeze_recovered) sont émis
    séparément par _process_cycle_events() à la fin du cycle, APRÈS avoir
    appliqué la règle de majorité ≥ 50% des zones figées.

    L'état est persisté sur /tmp entre invocations de python capture.py
    (nécessaire car chaque cycle = nouveau processus Python).

    Args:
        zone_name         : nom de la zone (clé de suivi)
        flow_bytes_list   : liste des bytes .pbf téléchargés pour cette zone
    """
    import hashlib

    if not flow_bytes_list:
        return

    combined = b"".join(flow_bytes_list)
    current = hashlib.md5(combined).hexdigest()[:12]
    previous = _last_flow_hash.get(zone_name)

    if previous == current:
        n = _consecutive_frozen.get(zone_name, 0) + 1
        _consecutive_frozen[zone_name] = n
        print(f"  ⚠ DONNÉES GELÉES : tuiles flow identiques au cycle précédent"
              f" (hash={current}, {n}× consécutifs) — gel API TomTom probable")
    else:
        prev_frozen = _consecutive_frozen.get(zone_name, 0)
        if prev_frozen > 0:
            print(f"  ✓ Données rafraîchies après {prev_frozen} cycle(s) gelé(s)")
        _consecutive_frozen[zone_name] = 0

    _last_flow_hash[zone_name] = current

    # Persister immédiatement pour le prochain cycle
    _save_freshness_state()


def _process_cycle_events(zone_names):
    """
    Appelée à la fin du cycle, après traitement de toutes les zones.

    Applique la règle de gel global (≥ 50% des zones avec consec_frozen ≥ 2)
    et émet des événements 'data_freeze' (ouverture) / 'data_freeze_recovered'
    (clôture) dans capture_errors.json. Une seule paire open/close par épisode,
    pas de spam à chaque cycle pendant un gel prolongé.
    """
    if not zone_names:
        return

    n_total = len(zone_names)
    n_frozen_2plus = sum(
        1 for z in zone_names if _consecutive_frozen.get(z, 0) >= 2
    )
    global_gel = (n_frozen_2plus / n_total) >= _GLOBAL_GEL_THRESHOLD
    now_iso = _now_utc_iso()

    for zone in zone_names:
        consec = _consecutive_frozen.get(zone, 0)
        is_open = _freeze_event_open.get(zone, False)

        # Ouverture : gel confirmé (≥ 2 cycles identiques ET majorité globale)
        if consec >= 2 and global_gel and not is_open:
            _first_frozen_at[zone] = now_iso
            _freeze_event_open[zone] = True
            _log_event(
                event_type="data_freeze",
                zone=zone,
                severity="warning",
                details={
                    "consecutive_frozen_cycles": consec,
                    "flow_hash": _last_flow_hash.get(zone, ""),
                    "global_gel_context": {
                        "frozen_zones_count": n_frozen_2plus,
                        "total_zones_count": n_total,
                        "threshold_met": True,
                    },
                },
            )
            print(f"  📝 Event loggé : data_freeze pour {zone}")

        # Clôture : zone retourne à fresh alors qu'un épisode était ouvert
        elif consec == 0 and is_open:
            started = _first_frozen_at.get(zone)
            duration_min = None
            if started:
                try:
                    t0 = datetime.strptime(
                        started, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                    t1 = datetime.now(timezone.utc)
                    duration_min = int((t1 - t0).total_seconds() / 60)
                except (ValueError, TypeError):
                    pass
            _log_event(
                event_type="data_freeze_recovered",
                zone=zone,
                severity="info",
                details={
                    "froze_from_utc": started,
                    "froze_to_utc": now_iso,
                    "duration_minutes": duration_min,
                },
            )
            _freeze_event_open[zone] = False
            _first_frozen_at[zone] = None
            print(f"  📝 Event loggé : data_freeze_recovered pour {zone}"
                  f" (durée {duration_min} min)")

    _save_freshness_state()


# ─── Notifications ntfy ──────────────────────────────────────────────────────
#
# Dispatch en fin de cycle selon le mode configuré par type d'événement
# (dict NTFY_NOTIFICATIONS dans config_ntfy.py — optionnel) :
#   - immediate        : notif à chaque cycle où l'event apparaît
#   - batch_per_cycle  : 1 notif/cycle résumant tous les events du type
#   - episode          : notif à l'ouverture (après N cycles consécutifs)
#                        puis silence, notif à la clôture (1 cycle sans l'event)
#
# État d'épisode persisté dans le JSON freshness sous la clé "episode_states",
# avec un dict par type d'event :
#   { "api_http_error": {
#       "streak_count": 2,                  # cycles consécutifs avec event présent
#       "streak_first_seen_at_utc": "..",   # début du streak actuel
#       "streak_event_sum": 107,            # total events pendant le streak
#       "episode_open": false,
#       "episode_start_at_utc": null,
#       "episode_events_total": 0,
#       "episode_cycles_total": 0,
#     }, ... }

_NTFY_TOPIC_URL = os.environ.get("NTFY_TOPIC_URL", "").strip()


def _send_ntfy_notification(title, body, priority=3, tags=None):
    """Envoi d'une notification ntfy. Silencieux en cas d'échec réseau.

    title, body : texte (UTF-8 dans le body, ASCII recommandé pour title/tags
                  car ce sont des en-têtes HTTP).
    priority    : 1 (min) à 5 (max) — contrôle son/vibration de l'app.
    tags        : liste de courts mots-clés (ntfy les rend en émojis si connus,
                  ex: "warning" → ⚠, "white_check_mark" → ✅).
    """
    if not _NTFY_TOPIC_URL:
        return  # Pas configuré → désactivé silencieusement

    try:
        headers = {
            "Title": title.encode("ascii", errors="replace").decode("ascii"),
            "Priority": str(priority),
        }
        if tags:
            headers["Tags"] = ",".join(tags)
        resp = requests.post(
            _NTFY_TOPIC_URL,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=5,
        )
        if resp.status_code >= 400:
            print(f"  ⚠ ntfy HTTP {resp.status_code} pour notif '{title}'")
        else:
            print(f"  📨 ntfy → {title}")
    except requests.exceptions.RequestException as e:
        print(f"  ⚠ ntfy échec ({type(e).__name__}) pour notif '{title}'")


def _format_zones_list(zones, max_zones=8):
    """Formate une liste de zones pour le corps d'une notif.

    Si > max_zones, tronque avec '(+N autres)' pour éviter des notifs trop longues.
    """
    if not zones:
        return "aucune zone identifiée"
    unique = list(dict.fromkeys(zones))  # dedupe en gardant l'ordre
    if len(unique) <= max_zones:
        return ", ".join(unique)
    return ", ".join(unique[:max_zones]) + f" (+{len(unique) - max_zones} autres)"


def _notif_immediate(event_type, events_this_cycle, cfg):
    """Mode immediate : 1 notif par cycle agrégée (toutes zones dans le body)."""
    if not events_this_cycle:
        return

    zones = [e.get("zone") for e in events_this_cycle if e.get("zone")]
    title = cfg.get("title", event_type)

    if event_type == "data_freeze":
        n = len(events_this_cycle)
        body = (
            f"Cycle {_current_local_time} CEST\n"
            f"{n} zone(s) figee(s) (gel API TomTom probable)\n\n"
            f"Zones : {_format_zones_list(zones)}"
        )
    elif event_type == "data_freeze_recovered":
        n = len(events_this_cycle)
        durations = [
            e["details"].get("duration_minutes")
            for e in events_this_cycle
            if e["details"].get("duration_minutes") is not None
        ]
        dur_info = f" (durée ~{max(durations)} min)" if durations else ""
        body = (
            f"Cycle {_current_local_time} CEST\n"
            f"{n} zone(s) ont retrouve des donnees fraiches{dur_info}\n\n"
            f"Zones : {_format_zones_list(zones)}"
        )
    elif event_type == "zone_capture_failed":
        n = len(events_this_cycle)
        details_summary = []
        for e in events_this_cycle:
            d = e.get("details", {})
            details_summary.append(
                f"- {e.get('zone')} : {d.get('exception_type', '?')}"
            )
        body = (
            f"Cycle {_current_local_time} CEST\n"
            f"{n} zone(s) en echec (exception capture_zone)\n\n"
            + "\n".join(details_summary)
        )
    else:
        body = (
            f"Cycle {_current_local_time} CEST\n"
            f"{len(events_this_cycle)} evenement(s)\n\n"
            f"Zones : {_format_zones_list(zones)}"
        )

    _send_ntfy_notification(
        title=title,
        body=body,
        priority=cfg.get("priority", 3),
        tags=cfg.get("tags", []),
    )


def _notif_batch(event_type, events_this_cycle, cfg):
    """Mode batch_per_cycle : 1 notif/cycle résumant tous les events."""
    if not events_this_cycle:
        return

    zones = [e.get("zone") for e in events_this_cycle if e.get("zone")]
    n = len(events_this_cycle)
    title = cfg.get("title", event_type)

    body = (
        f"Cycle {_current_local_time} CEST\n"
        f"{n} evenement(s) detecte(s) sur {len(set(zones))} zone(s)\n\n"
        f"Zones : {_format_zones_list(zones)}"
    )

    _send_ntfy_notification(
        title=title,
        body=body,
        priority=cfg.get("priority", 2),
        tags=cfg.get("tags", []),
    )


def _notif_episode(event_type, events_this_cycle, cfg, episode_state):
    """Mode episode : suivi de streak avec ouverture après N cycles consécutifs.

    Met à jour episode_state en place et envoie les notifs d'ouverture / clôture
    quand les seuils sont atteints.
    Reset dur : 1 cycle sans event remet streak_count à 0.
    Clôture : 1 cycle sans event suffit pour clôturer un épisode ouvert.
    """
    threshold = NTFY_EPISODE_OPEN_THRESHOLD
    event_count = len(events_this_cycle)
    zones = list(dict.fromkeys(
        e.get("zone") for e in events_this_cycle if e.get("zone")
    ))

    if event_count > 0:
        # Apparition de l'event sur ce cycle
        if episode_state["streak_count"] == 0:
            # Nouveau streak — premier cycle d'apparition
            episode_state["streak_first_seen_at_utc"] = _now_utc_iso()
            episode_state["streak_event_sum"] = 0
        episode_state["streak_count"] += 1
        episode_state["streak_event_sum"] += event_count

        if episode_state["episode_open"]:
            # Épisode déjà ouvert — on cumule juste, pas de notif
            episode_state["episode_events_total"] += event_count
            episode_state["episode_cycles_total"] += 1

        elif episode_state["streak_count"] >= threshold:
            # Seuil atteint → OUVERTURE de l'épisode
            episode_state["episode_open"] = True
            episode_state["episode_start_at_utc"] = episode_state["streak_first_seen_at_utc"]
            episode_state["episode_events_total"] = episode_state["streak_event_sum"]
            episode_state["episode_cycles_total"] = episode_state["streak_count"]

            # Notif d'ouverture
            title = cfg.get("title_open", cfg.get("title", event_type))
            start_time = episode_state["episode_start_at_utc"]
            body = (
                f"Detecte sur {threshold} cycles consecutifs.\n"
                f"Debut observe : {start_time} UTC\n"
                f"Cycle actuel ({_current_local_time}) : {event_count} evenement(s)\n\n"
                f"Zones touchees ce cycle : {_format_zones_list(zones)}\n\n"
                f"Total depuis debut : {episode_state['episode_events_total']} evenement(s)\n"
                f"Note : pas de nouvelle notif avant la resolution."
            )
            _send_ntfy_notification(
                title=title,
                body=body,
                priority=cfg.get("priority", 4),
                tags=cfg.get("tags", []),
            )

    else:
        # Pas d'event de ce type ce cycle — reset dur du streak
        if episode_state["episode_open"]:
            # CLÔTURE de l'épisode
            episode_state["episode_open"] = False
            title = cfg.get("title_close", cfg.get("title", event_type))

            # Calcul de la durée
            start = episode_state.get("episode_start_at_utc")
            duration_str = "inconnue"
            if start:
                try:
                    t0 = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    t1 = datetime.now(timezone.utc)
                    total_min = int((t1 - t0).total_seconds() / 60)
                    if total_min < 60:
                        duration_str = f"{total_min} min"
                    else:
                        h = total_min // 60
                        m = total_min % 60
                        duration_str = f"{h}h{m:02d}"
                except (ValueError, TypeError):
                    pass

            body = (
                f"Episode resolu au cycle {_current_local_time} CEST.\n\n"
                f"Duree : {duration_str}\n"
                f"Cycles affectes : {episode_state['episode_cycles_total']}\n"
                f"Total evenements : {episode_state['episode_events_total']}"
            )
            _send_ntfy_notification(
                title=title,
                body=body,
                priority=cfg.get("priority", 3),
                tags=["white_check_mark"],
            )

            # Reset complet des compteurs d'épisode
            episode_state["episode_start_at_utc"] = None
            episode_state["episode_events_total"] = 0
            episode_state["episode_cycles_total"] = 0

        # Reset dur du streak (toujours, même si épisode pas ouvert)
        episode_state["streak_count"] = 0
        episode_state["streak_first_seen_at_utc"] = None
        episode_state["streak_event_sum"] = 0


def _process_cycle_notifications():
    """Dispatch les notifications ntfy en fin de cycle.

    Appelée APRÈS _process_cycle_events (qui a déjà écrit data_freeze /
    data_freeze_recovered dans _current_cycle_events_buffer via _log_event).

    Silencieux si :
      - NTFY_TOPIC_URL n'est pas configuré dans l'environnement, OU
      - config_ntfy.py n'a pas pu être importé (NTFY_NOTIFICATIONS vide)
    """
    if not _NTFY_TOPIC_URL or not NTFY_NOTIFICATIONS:
        return

    # Grouper les events du cycle par type
    by_type = {}
    for event in _current_cycle_events_buffer:
        by_type.setdefault(event["type"], []).append(event)

    # Charger l'état des épisodes (étendu dans le state freshness)
    state = _load_freshness_state()
    episode_states = state.get("episode_states", {})

    for event_type, cfg in NTFY_NOTIFICATIONS.items():
        if not cfg.get("enabled", False):
            continue

        events_this_cycle = by_type.get(event_type, [])
        mode = cfg.get("mode", "immediate")

        if mode == "immediate":
            _notif_immediate(event_type, events_this_cycle, cfg)

        elif mode == "batch_per_cycle":
            _notif_batch(event_type, events_this_cycle, cfg)

        elif mode == "episode":
            episode_state = episode_states.setdefault(event_type, {
                "streak_count": 0,
                "streak_first_seen_at_utc": None,
                "streak_event_sum": 0,
                "episode_open": False,
                "episode_start_at_utc": None,
                "episode_events_total": 0,
                "episode_cycles_total": 0,
            })
            _notif_episode(event_type, events_this_cycle, cfg, episode_state)

    # Persister le state complet (avec episode_states mis à jour)
    try:
        _FRESHNESS_STATE_FILE.write_text(json.dumps({
            "hashes": _last_flow_hash,
            "consecutive_frozen": _consecutive_frozen,
            "freeze_event_open": _freeze_event_open,
            "first_frozen_at_utc": _first_frozen_at,
            "episode_states": episode_states,
        }))
    except OSError:
        pass


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


def download_base_image(lat, lon, zoom, width, height, api_key, zone_name=None):
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
    data = api_get(url, binary=True, zone_name=zone_name, api_layer="base")
    if data:
        img = Image.open(BytesIO(data)).convert("RGBA")
        if img.size != (width, height):
            img = img.resize((width, height), Image.LANCZOS)
        if BASE_MAP_STYLE == "light":
            img = _apply_light_style(img)
        return img
    return None


def resolve_road_types(zone_config, zoom):
    """
    Retourne la liste effective des indices de types de routes pour une zone
    (appliqué uniquement au flow via l'API TomTom).

    Priorité :
      1. Clé 'road_types_override' dans zone_config → surcharge explicite
      2. ROAD_TYPES_BY_ZOOM[zoom]                   → fallback par zoom
      3. [0, 1, 2, 3]                                → fallback par défaut
    """
    if "road_types_override" in zone_config:
        return zone_config["road_types_override"]
    return ROAD_TYPES_BY_ZOOM.get(zoom, [0, 1, 2, 3])


def download_vector_flow(lat, lon, zoom, width, height, api_key, road_types,
                         tile_render_size=None, zone_name=None):
    """Télécharge et dessine les tuiles vectorielles de trafic flow (.pbf).

    road_types : liste d'indices de types de routes à capturer, résolue en amont
                 par resolve_road_types() (surcharge par zone ou fallback zoom).
    zone_name  : si fourni, déclenche le contrôle de fraîcheur des données
                 (détection de gel API TomTom).
    """
    if tile_render_size is None:
        tile_render_size = TILE_SIZE

    road_types_param = "[" + ",".join(str(r) for r in road_types) + "]"

    tiles, origin_px, origin_py = get_tile_grid(
        lat, lon, zoom, width, height, tile_render_size
    )

    flow_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(flow_img)

    n_tiles = len(tiles)
    n_features = 0
    n_downloaded = 0

    # Collecte des octets .pbf pour le contrôle de fraîcheur API
    raw_tile_bytes = []

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

        data = api_get(url, binary=True, zone_name=zone_name, api_layer="flow")
        if not data:
            continue
        n_downloaded += 1
        raw_tile_bytes.append(data)

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

    # Contrôle de fraîcheur — alerte si l'API TomTom sert des données gelées
    if zone_name:
        _check_data_freshness(zone_name, raw_tile_bytes)

    # Événement structuré si des tuiles ont manqué
    if zone_name and n_downloaded < n_tiles:
        _log_event(
            event_type="partial_capture",
            zone=zone_name,
            severity="warning",
            details={
                "api_layer": "flow",
                "downloaded": n_downloaded,
                "expected": n_tiles,
                "missing": n_tiles - n_downloaded,
            },
        )

    print(f"  🚗 Flow: {n_downloaded}/{n_tiles} tuiles, {n_features} segments"
          f" (roadTypes={road_types})")
    return flow_img


def download_incidents(lat, lon, zoom, width, height, api_key,
                       tile_render_size=None, collect_annotations=False,
                       zone_name=None, collect_structured=False):
    """
    Télécharge les incidents via Vector Incident Tiles (.pbf) et les dessine.

    Si collect_annotations=True, retourne (image, annotations_list) où
    annotations_list contient les données nécessaires pour dessiner les badges.
    Sinon, retourne (image, []).

    Si collect_structured=True, retourne (image, annotations_list, structured_list)
    où structured_list contient un dict par incident unique (dédupliqué par
    tomtom_id) avec la polyline reprojetée en WGS84, prêt pour le fichier JSON
    d'export utilisé par l'onglet Analyse (v14).

    zone_name : si fourni, utilisé pour le contexte des événements loggés
                (timeouts, HTTP errors, partial captures).
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

    # Pour l'export structuré : dict {tomtom_id → record}, on garde la polyline
    # la plus longue par tomtom_id (heuristique pragmatique pour la dédup
    # inter-tuiles). La dédup inter-zones est faite plus tard.
    structured_by_id = {} if (collect_structured and INCIDENTS_EXPORT_ENABLED) else None

    for tile_x, tile_y, px_off, py_off in tiles:
        max_tile = 2 ** zoom - 1
        if tile_y < 0 or tile_y > max_tile:
            continue
        tx = tile_x % (max_tile + 1)

        url = (
            f"https://api.tomtom.com/traffic/map/4/tile/incidents"
            f"/{zoom}/{tx}/{tile_y}.pbf"
            f"?key={api_key}"
            f"&tags=[icon_category,description,magnitude,road_type,delay,id,"
            f"end_date,last_report_time,road_category,road_subcategory,"
            f"traffic_road_coverage,probability_of_occurrence,number_of_reports,clustered]"
        )

        data = api_get(url, binary=True, zone_name=zone_name, api_layer="incidents")
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

                # Collecte structurée pour l'export JSON (v14 préparatoire) —
                # On reprojette en lat/lon directement depuis les coords MVT brutes,
                # plus précis que de passer par les pixels d'image arrondis.
                if structured_by_id is not None and incident_id is not None:
                    polyline_latlon = inc_export.reproject_mvt_polyline(
                        line, tile_x, tile_y, zoom, extent,
                    )
                    if len(polyline_latlon) >= 2:
                        existing = structured_by_id.get(incident_id)
                        if existing is None or len(polyline_latlon) > len(existing["polyline_latlon"]):
                            extended_tags = inc_export.extract_extended_tags(tags)
                            structured_by_id[incident_id] = inc_export.build_incident_record(
                                tomtom_id=incident_id,
                                icon_cat=icon_cat,
                                magnitude=magnitude,
                                delay=delay,
                                road_type=road_type,
                                polyline_latlon=polyline_latlon,
                                **extended_tags,
                            )

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

    # Événement structuré si des tuiles ont manqué
    if zone_name and n_downloaded < n_tiles:
        _log_event(
            event_type="partial_capture",
            zone=zone_name,
            severity="warning",
            details={
                "api_layer": "incidents",
                "downloaded": n_downloaded,
                "expected": n_tiles,
                "missing": n_tiles - n_downloaded,
            },
        )

    if collect_structured:
        structured_list = list(structured_by_id.values()) if structured_by_id else []
        return inc_img, annotations, structured_list
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
    road_types = resolve_road_types(zone_config, zoom)
    rt_source = "override zone" if "road_types_override" in zone_config else f"fallback zoom {zoom}"

    print(f"\n{'─'*60}")
    print(f"[{zone_name}] lat={lat} lon={lon} zoom={zoom}"
          f" annotations={'ON' if want_annotations else 'OFF'}")
    print(f"  📋 roadTypes flow={road_types} ({rt_source})")

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
        base_img = download_base_image(
            lat, lon, zoom, VIEWPORT_WIDTH, VIEWPORT_HEIGHT, api_key,
            zone_name=zone_name,
        )
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
        lat, lon, zoom, render_w, render_h, api_key, road_types,
        tile_render_size=tile_render_size,
        zone_name=zone_name,
    )

    # 3. Incidents (+ collecte des annotations si activé)
    annotations = []
    structured_incidents = []
    if INCIDENTS_ENABLED:
        if INCIDENTS_EXPORT_ENABLED:
            inc_img, annotations, structured_incidents = download_incidents(
                lat, lon, zoom, render_w, render_h, api_key,
                tile_render_size=tile_render_size,
                collect_annotations=want_annotations,
                zone_name=zone_name,
                collect_structured=True,
            )
        else:
            inc_img, annotations = download_incidents(
                lat, lon, zoom, render_w, render_h, api_key,
                tile_render_size=tile_render_size,
                collect_annotations=want_annotations,
                zone_name=zone_name,
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

    return structured_incidents


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

        base_img = download_base_image(
            lat, lon, zoom, VIEWPORT_WIDTH, VIEWPORT_HEIGHT, api_key,
            zone_name=zone_name,
        )
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
        road_types = resolve_road_types(zone_config, zoom)

        flow = n_tiles
        inc = n_tiles
        base = 1
        per_cycle = flow + inc

        ann_tag = " 🏷" if zone_config.get("annotations", False) else ""
        override_tag = " 🔧" if "road_types_override" in zone_config else ""
        print(f"  {name} (zoom={zoom}, roadTypes={road_types}){override_tag}{ann_tag}")
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

    # Setter le contexte pour le journal d'événements
    # (toutes les fonctions qui appellent _log_event() l'utilisent)
    _current_date_str = now.strftime("%Y-%m-%d")
    _current_local_time = now.strftime("%H:%M")

    # Afficher les zones configurées
    print("\nZones configurées:")
    for name, zone_config in ZONES.items():
        lat, lon, zoom = parse_zone_url(zone_config["url"])
        road_types = resolve_road_types(zone_config, zoom)
        ann = "✓" if zone_config.get("annotations", False) else "X"
        override_tag = " (override)" if "road_types_override" in zone_config else ""
        print(f"  ✓ {name}: zoom={zoom} roadTypes={road_types}{override_tag} annotations={ann}")

    print_budget_report()
    clear_stale_cache()

    if args.save_bases:
        save_bases(api_key)
    else:
        counter = 0
        errors = 0
        # Collecte par zone des incidents structurés du cycle courant (v14)
        cycle_records_by_zone = {}

        for zone_name, zone_config in ZONES.items():
            try:
                zone_records = capture_zone(zone_name, zone_config, api_key, now)
                if zone_records:
                    cycle_records_by_zone[zone_name] = zone_records
            except Exception as e:
                print(f"[{zone_name}] ✗ Erreur: {e}")
                import traceback
                traceback.print_exc()
                errors += 1
                _log_event(
                    event_type="zone_capture_failed",
                    zone=zone_name,
                    severity="error",
                    details={
                        "exception_type": type(e).__name__,
                        "message": str(e)[:300],
                    },
                )

        # Export JSON des incidents du cycle (v14 préparatoire)
        # Toujours appelé même si cycle_records_by_zone est vide : on veut une
        # entrée "incidents: []" pour matérialiser le cycle dans le fichier.
        if INCIDENTS_EXPORT_ENABLED and INCIDENTS_ENABLED:
            try:
                date_str = now.strftime("%Y-%m-%d")
                cycle_key = now.strftime("%H%M")
                datetime_utc = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                n_inc, n_zones = inc_export.save_cycle_to_day_file(
                    date_str=date_str,
                    zones=list(ZONES.keys()),
                    cycle_key=cycle_key,
                    datetime_utc=datetime_utc,
                    cycle_records_by_zone=cycle_records_by_zone,
                    output_dir=OUTPUT_DIR,
                )
                print(f"\n📊 Export incidents JSON : {n_inc} incidents uniques "
                      f"({n_zones}/{len(ZONES)} zones avec data) "
                      f"→ {OUTPUT_DIR}/{date_str}/incidents_{date_str}.json")
            except Exception as e:
                # On ne fait pas planter le run pour ça — les images sont déjà sauvées
                print(f"⚠ Échec export JSON incidents : {e}")
                import traceback
                traceback.print_exc()

        # Évaluer le gel global après traitement de toutes les zones
        # (règle ≥ 50% → émet data_freeze / data_freeze_recovered dans le log)
        try:
            _process_cycle_events(list(ZONES.keys()))
        except Exception as e:
            print(f"⚠ Échec évaluation gel global : {e}")

        # Dispatch des notifications ntfy
        # (silencieux si NTFY_TOPIC_URL absent ou config_ntfy.py non importé)
        try:
            _process_cycle_notifications()
        except Exception as e:
            print(f"⚠ Échec dispatch ntfy : {e}")

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
