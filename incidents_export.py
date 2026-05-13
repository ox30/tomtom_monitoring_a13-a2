"""
TomTom Traffic Capture — Export structuré des incidents (v14 préparatoire)
==========================================================================

Génère un fichier JSON par jour contenant tous les incidents capturés, organisés
par cycle, avec coordonnées WGS84 et déduplication par tomtom_id (inter-tuiles
et inter-zones).

Le fichier est écrit à la racine du dossier du jour, au même niveau que
`capture_errors.json` :

  captures/
    2026-04-27/
      zone_GST/                    ← existant
        *.jpg
      zone_globale_A2_A13/         ← existant
        *.jpg
      capture_errors.json          ← existant
      incidents_2026-04-27.json    ← NOUVEAU

Cette structure plate est volontaire :
- Aucune modification de Archive.yml nécessaire (le `find -type d` ne change pas)
- Le fichier est zippé automatiquement par `zip -r captures/${DATE}/`
- Cohérent avec le traitement existant de `capture_errors.json`

Référence : v14_cahier_des_charges.md, section 3
"""

import json
import math
import os
import tempfile
from pathlib import Path


# ─── Tables de référence (labels lisibles) ────────────────────────────────────
# Source : TomTom Vector Incident Tiles documentation
# https://developer.tomtom.com/traffic-api/documentation/traffic-incidents/vector-incident-tiles

ICON_CATEGORY_LABELS = {
    0:  "Unknown",
    1:  "Accident",
    2:  "Fog",
    3:  "Dangerous Conditions",
    4:  "Rain",
    5:  "Ice",
    6:  "Jam",
    7:  "Lane Closed",
    8:  "Road Closed",
    9:  "Road Works",
    10: "Wind",
    11: "Flooding",
    13: "Cluster",
    14: "Broken Down Vehicle",
}

MAGNITUDE_LABELS = {
    0: "Unknown",
    1: "Minor",
    2: "Moderate",
    3: "Major",
    4: "Indefinite",
}

SCHEMA_VERSION = 1
TOMTOM_API_VERSION = "4"


# ─── Extraction des tags étendus depuis le parsing MVT ────────────────────────

# Tags scalaires de type string (extraits tels quels s'ils sont présents)
_EXTENDED_STRING_TAGS = (
    "description",
    "end_date",
    "last_report_time",
    "road_category",
    "road_subcategory",
    "traffic_road_coverage",
    "probability_of_occurrence",
)

# Tags scalaires de type int (parsés avec gestion d'erreur silencieuse)
_EXTENDED_INT_TAGS = (
    "number_of_reports",
    "clustered",
)


def extract_extended_tags(tags):
    """
    Extrait les tags enrichis (au-delà des 5 tags de base) depuis un dict de
    propriétés issu du parsing MVT TomTom.

    Retourne un dict ne contenant que les champs effectivement présents et
    valides — destiné à être passé en **kwargs à build_incident_record().

    Tags traités :
    - icon_category_[idx] et description_[idx] (collectés par index croissant)
    - description, end_date, last_report_time, road_category, road_subcategory,
      traffic_road_coverage, probability_of_occurrence  (strings)
    - number_of_reports, clustered  (ints)
    """
    out = {}

    # Tags indexés : icon_category_0, icon_category_1, ... + description_0, ...
    icon_cats_by_idx = {}
    descs_by_idx = {}
    for key, val in tags.items():
        if key.startswith("icon_category_"):
            try:
                idx = int(key.rsplit("_", 1)[1])
                icon_cats_by_idx[idx] = int(val)
            except (ValueError, TypeError, IndexError):
                pass
        elif key.startswith("description_"):
            try:
                idx = int(key.rsplit("_", 1)[1])
                descs_by_idx[idx] = str(val)
            except (ValueError, TypeError, IndexError):
                pass

    if icon_cats_by_idx:
        # Tri par index = ordre de priorité croissante selon la spec TomTom
        out["icon_categories_all"] = [icon_cats_by_idx[i] for i in sorted(icon_cats_by_idx)]
    if descs_by_idx:
        out["descriptions_all"] = [descs_by_idx[i] for i in sorted(descs_by_idx)]

    # Tags scalaires de type string
    for key in _EXTENDED_STRING_TAGS:
        val = tags.get(key)
        if val is not None:
            out[key] = val if isinstance(val, str) else str(val)

    # Tags scalaires de type int
    for key in _EXTENDED_INT_TAGS:
        val = tags.get(key)
        if val is not None:
            try:
                out[key] = int(val)
            except (ValueError, TypeError):
                pass

    return out


# ─── Reprojection de coordonnées ──────────────────────────────────────────────

def tile_to_lat_lon(tile_x_float, tile_y_float, zoom):
    """
    Convertit des coordonnées de tuile flottantes en WGS84 (lat, lon).

    Formule inverse Web Mercator (slippy map), exactement complémentaire de
    `lat_lon_to_tile()` présente dans capture.py.

    Arguments :
        tile_x_float : position X flottante dans la grille de tuiles à ce zoom
                       (par exemple : tile_x + tx_coord_pbf / extent)
        tile_y_float : position Y flottante
        zoom         : niveau de zoom (entier)

    Retourne : (lat, lon) en degrés WGS84
    """
    n = 2.0 ** zoom
    lon_deg = tile_x_float / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * tile_y_float / n)))
    lat_deg = math.degrees(lat_rad)
    return lat_deg, lon_deg


def reproject_mvt_polyline(mvt_polyline, tile_x, tile_y, zoom, extent):
    """
    Reprojette une polyline MVT (coords brutes du parseur) en coordonnées WGS84.

    Arguments :
        mvt_polyline : liste de tuples (tx_coord, ty_coord) issue du parseur MVT
                       (range typiquement 0..extent, mais peut sortir avec la marge)
        tile_x, tile_y : coordonnées de la tuile MVT d'origine
        zoom           : niveau de zoom
        extent         : `extent` de la feature (typiquement 4096)

    Retourne : liste de [lat, lon] (listes pour sérialisation JSON propre)
    """
    if extent <= 0:
        extent = 4096
    out = []
    for tx_coord, ty_coord in mvt_polyline:
        tx_float = tile_x + tx_coord / extent
        ty_float = tile_y + ty_coord / extent
        lat, lon = tile_to_lat_lon(tx_float, ty_float, zoom)
        out.append([round(lat, 6), round(lon, 6)])
    return out


def polyline_midpoint_latlon(polyline_latlon):
    """
    Retourne le point milieu (par longueur cumulée) d'une polyline lat/lon.
    Utilisé comme point d'ancrage pour les marqueurs cliquables / clustering.

    Calcul euclidien en degrés — suffisant pour les courtes distances
    autoroutières suisses (la déformation Web Mercator est négligeable).
    """
    if not polyline_latlon:
        return [0.0, 0.0]
    if len(polyline_latlon) == 1:
        return list(polyline_latlon[0])

    # Longueurs des segments
    seg_lengths = []
    total = 0.0
    for i in range(len(polyline_latlon) - 1):
        a = polyline_latlon[i]
        b = polyline_latlon[i + 1]
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        seg_lengths.append(d)
        total += d

    if total < 1e-9:
        return list(polyline_latlon[0])

    target = total / 2.0
    accumulated = 0.0
    for i, seg_len in enumerate(seg_lengths):
        if accumulated + seg_len >= target:
            remaining = target - accumulated
            t = remaining / seg_len if seg_len > 0 else 0
            a = polyline_latlon[i]
            b = polyline_latlon[i + 1]
            lat = a[0] + t * (b[0] - a[0])
            lon = a[1] + t * (b[1] - a[1])
            return [round(lat, 6), round(lon, 6)]
        accumulated += seg_len

    return list(polyline_latlon[-1])


# ─── Construction d'un record d'incident ──────────────────────────────────────

# Champs étendus passés tels quels dans le record final (si présents).
# `description`, `end_date` sont aussi acceptés mais traités séparément pour la
# rétrocompatibilité avec les premiers appels existants (signature historique).
_PASS_THROUGH_FIELDS = (
    "descriptions_all",
    "last_report_time",
    "road_category",
    "road_subcategory",
    "traffic_road_coverage",
    "probability_of_occurrence",
    "number_of_reports",
    "clustered",
)


def build_incident_record(*, tomtom_id, icon_cat, magnitude, delay, road_type,
                          polyline_latlon, description=None, end_date=None,
                          icon_categories_all=None, **extended):
    """
    Construit un dict d'incident structuré au format JSON.

    Champs principaux (toujours présents) :
      tomtom_id, icon_category, icon_category_label, magnitude, magnitude_label,
      delay_s, road_type, polyline_latlon, center_latlon
    (`seen_in_zones` est ajouté par merge_zone_records_into_cycle.)

    Champs étendus facultatifs (v14 enrichi) — inclus seulement si fournis :
      icon_categories_all, icon_categories_labels_all
      descriptions_all
      description, end_date, last_report_time
      road_category, road_subcategory
      traffic_road_coverage, probability_of_occurrence
      number_of_reports, clustered

    Le caller construit ces champs via extract_extended_tags() et les passe
    en **kwargs.
    """
    record = {
        "tomtom_id": tomtom_id,
        "icon_category": icon_cat,
        "icon_category_label": ICON_CATEGORY_LABELS.get(icon_cat, f"Unknown({icon_cat})"),
        "magnitude": magnitude,
        "magnitude_label": MAGNITUDE_LABELS.get(magnitude, f"Unknown({magnitude})"),
        "delay_s": delay,
        "road_type": road_type,
        "polyline_latlon": polyline_latlon,
        "center_latlon": polyline_midpoint_latlon(polyline_latlon),
    }

    # icon_categories_all : liste des catégories ordonnées par priorité croissante
    # On y ajoute systématiquement les labels lisibles pour faciliter exploitation.
    if icon_categories_all:
        record["icon_categories_all"] = list(icon_categories_all)
        record["icon_categories_labels_all"] = [
            ICON_CATEGORY_LABELS.get(c, f"Unknown({c})") for c in icon_categories_all
        ]

    # description / end_date : signature historique, conservée pour compat
    if description:
        record["description"] = description
    if end_date:
        record["end_date"] = end_date

    # Tous les autres champs étendus
    for key in _PASS_THROUGH_FIELDS:
        if key in extended and extended[key] is not None:
            record[key] = extended[key]

    return record


# ─── Déduplication inter-zones au sein d'un cycle ─────────────────────────────

def merge_zone_records_into_cycle(cycle_incidents_by_id, zone_name, zone_records):
    """
    Ajoute les records d'une zone à la liste cumulée d'incidents du cycle,
    en dédupliquant par `tomtom_id` :
      - Si un tomtom_id n'a jamais été vu : ajout pur, `seen_in_zones = [zone]`
      - Si déjà vu :
          * on ajoute la zone à `seen_in_zones`
          * on garde le record dont la polyline est la plus longue (= le plus
            informatif géographiquement). C'est une heuristique pragmatique
            pour la v1 ; une fusion propre des segments serait complexe.

    Arguments :
        cycle_incidents_by_id : dict {tomtom_id → record} (mutated in place)
        zone_name             : nom de la zone qu'on intègre
        zone_records          : liste de records produits par cette zone

    Pas de valeur de retour (modification in-place).
    """
    for record in zone_records:
        tid = record.get("tomtom_id")
        if not tid:
            # Sans ID stable, impossible de dédupliquer proprement → on skip.
            # Ces incidents existeront toujours sur les images, mais pas dans le JSON.
            continue

        existing = cycle_incidents_by_id.get(tid)
        if existing is None:
            new_record = dict(record)
            new_record["seen_in_zones"] = [zone_name]
            cycle_incidents_by_id[tid] = new_record
        else:
            # Ajouter la zone si pas déjà listée
            if zone_name not in existing["seen_in_zones"]:
                existing["seen_in_zones"].append(zone_name)

            # Garder la polyline la plus longue (= plus informatif)
            new_len = len(record.get("polyline_latlon", []))
            existing_len = len(existing.get("polyline_latlon", []))
            if new_len > existing_len:
                seen = existing["seen_in_zones"]
                existing.update(record)
                existing["seen_in_zones"] = seen
                existing["center_latlon"] = polyline_midpoint_latlon(
                    existing["polyline_latlon"]
                )


# ─── I/O JSON : lecture, mise à jour atomique, écriture ───────────────────────

def _day_file_path(date_str, output_dir):
    """Chemin du fichier d'incidents du jour."""
    return Path(output_dir) / date_str / f"incidents_{date_str}.json"


def _load_or_init_day_file(filepath, date_str, zones):
    """
    Lit le JSON existant ou crée la structure de base si absent.
    Robuste aux fichiers corrompus : si JSON invalide, repart d'une structure vide
    en loguant l'incident sur stdout (pas de crash du cycle de capture).
    """
    if filepath.exists():
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Validation minimale du schéma
            if isinstance(data, dict) and "cycles" in data and "metadata" in data:
                return data
            print(f"  ⚠ {filepath.name} a un format inattendu — réinitialisation")
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ⚠ {filepath.name} illisible ({e}) — réinitialisation")

    return {
        "metadata": {
            "date": date_str,
            "schema_version": SCHEMA_VERSION,
            "zones": list(zones),
            "tomtom_api_version": TOMTOM_API_VERSION,
        },
        "cycles": {},
    }


def _atomic_write_json(filepath, data):
    """
    Écriture atomique : on écrit dans un fichier temporaire dans le même dossier,
    puis on renomme (os.replace est atomique sur POSIX et Windows récent).
    Évite la corruption si le process est tué en plein milieu de l'écriture.
    """
    filepath.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=filepath.name + ".",
        suffix=".tmp",
        dir=str(filepath.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
    except Exception:
        # Nettoyage du temp en cas d'échec
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_cycle_to_day_file(*, date_str, zones, cycle_key, datetime_utc,
                           cycle_records_by_zone, output_dir):
    """
    Point d'entrée principal — appelé une fois par cycle, après que toutes les
    zones ont été capturées.

    Lit le JSON du jour s'il existe, ajoute (ou remplace) le cycle courant
    avec les incidents agrégés et dédupliqués entre zones, puis écrit
    atomiquement le fichier.

    Arguments :
        date_str              : "YYYY-MM-DD" (date locale Zurich, cohérent avec
                                les dossiers de captures)
        zones                 : liste des noms de zones (pour metadata)
        cycle_key             : "HHMM" en heure locale Zurich (cohérent avec
                                les noms de fichiers images)
        datetime_utc          : timestamp ISO 8601 UTC du cycle (ex. "2026-04-27T07:06:00Z")
        cycle_records_by_zone : dict {zone_name → list_of_records}
        output_dir            : Path du dossier captures/ (config.OUTPUT_DIR)

    Retourne : tuple (n_incidents_uniques, n_zones_avec_data) pour le log
    """
    filepath = _day_file_path(date_str, output_dir)
    day_data = _load_or_init_day_file(filepath, date_str, zones)

    # Mise à jour des metadata (zones peut évoluer, ex. ajout d'une zone)
    existing_zones = set(day_data["metadata"].get("zones", []))
    declared_zones = set(zones)
    if existing_zones != declared_zones:
        # Union — on garde tout, c'est plus prudent (on n'oublie pas des zones
        # qui ont produit des incidents plus tôt dans la journée et qu'on a depuis
        # désactivées)
        day_data["metadata"]["zones"] = sorted(existing_zones | declared_zones)

    # Mettre à jour la cycles_count si présent (pour info)
    # On ne tente pas de prédire 144 — on compte juste les cycles déjà présents

    # Dédupliquer inter-zones par tomtom_id
    cycle_by_id = {}
    n_zones_with_data = 0
    for zone_name, records in cycle_records_by_zone.items():
        if records:
            n_zones_with_data += 1
        merge_zone_records_into_cycle(cycle_by_id, zone_name, records)

    # Construire le cycle (liste triée par tomtom_id pour reproductibilité)
    sorted_incidents = sorted(cycle_by_id.values(), key=lambda r: r["tomtom_id"])

    day_data["cycles"][cycle_key] = {
        "datetime_utc": datetime_utc,
        "incidents": sorted_incidents,
    }

    # Mettre à jour le compteur de cycles (utile pour debugging / vue d'ensemble)
    day_data["metadata"]["cycles_count"] = len(day_data["cycles"])

    _atomic_write_json(filepath, day_data)

    return len(sorted_incidents), n_zones_with_data
