"""
Configuration — TomTom Traffic Capture
=======================================
Tous les paramètres du système sont centralisés ici.
Modifier ce fichier pour ajuster les zones, couleurs, épaisseurs, annotations, etc.

capture.py importe ce fichier — ne pas renommer.
"""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

# ─── Zones de capture ─────────────────────────────────────────────────────────
# Coller directement l'URL de plan.tomtom.com.
# Le zoom fractionnaire est arrondi automatiquement.
#
# annotations : True  → afficher les badges de délai sur les incidents annotés
#               False → pas de badges (utile pour les vues larges / zoom faible)

ZONES = {
    "zone_GST_Nord": {
        "url": "https://plan.tomtom.com/en/?p=46.82968,8.60058,10z",
        "annotations": True,
    },
    "zone_Monitoring_2026": {
        "url": "https://plan.tomtom.com/en/?p=46.89357,9.49312,10z",
        "annotations": True,
    },
    "zone_Sargans-Landquart": {
        "url": "https://plan.tomtom.com/en/?p=47.0149,9.50575,12z",
        "annotations": True,
    },
    "zone_Landquart-Chur": {
        "url": "https://plan.tomtom.com/en/?p=46.92199,9.52654,12z",
        "annotations": True,
    },
    "zone_Chur_Isla-T": {
        "url": "https://plan.tomtom.com/en/?p=46.81697,9.48109,12z",
        "annotations": True,
    },
    "zone_GST_Sud": {
        "url": "https://plan.tomtom.com/en/?p=46.45552,8.76656,11z",
        "annotations": True,
    },
}

# ─── Dimensions et chemins ────────────────────────────────────────────────────

VIEWPORT_WIDTH  = 1920
VIEWPORT_HEIGHT = 1080
TILE_SIZE       = 512       # Taille des tuiles vectorielles (en pixels de rendu)
RETENTION_DAYS  = 7
OUTPUT_DIR      = Path("captures")    # Captures toutes les 10 min
BASES_DIR       = Path("bases")       # Cartes de base 1× par run
CACHE_DIR       = Path(".base-cache") # Cache local (pas commité)
TIMEZONE        = ZoneInfo("Europe/Zurich")

# ─── Supersampling ────────────────────────────────────────────────────────────
# Rendu interne à N× la résolution finale, puis downscale LANCZOS.
# Produit des lignes anti-aliasées (lissées) sans aucun surcoût API.
# 1 = désactivé   |  2 = bon compromis   |  3 = meilleur rendu, RAM ×3
SUPERSAMPLE = 2

# ─── Format de sortie ─────────────────────────────────────────────────────────
OUTPUT_QUALITY = 95     # Qualité JPEG (88-95 recommandé)

# ─── Style de la carte de base ────────────────────────────────────────────────
# "main"  → couleurs standard TomTom
# "light" → fond gris clair désaturé
# "night" → mode nuit (sombre)
BASE_MAP_STYLE   = os.environ.get("BASE_MAP_STYLE", "night")
LIGHT_SATURATION = 0.05    # 0.0 = gris pur, 1.0 = couleurs originales
LIGHT_BRIGHTNESS = 1.1     # > 1.0 = plus clair
LIGHT_CONTRAST   = 1.5     # > 1.0 = plus contrasté

# ─── Filtrage des routes par zoom ─────────────────────────────────────────────
# Plus le zoom est faible (vue large), moins on affiche de routes
# Types: 0=Motorway 1=International 2=Major 3=Secondary 4=Connecting
#        5=MajorLocal 6=Local 7=MinorLocal
ROAD_TYPES_BY_ZOOM = {
    8:  [0, 1],
    9:  [0, 1],
    10: [0, 1, 2],
    11: [0, 1, 2, 3],
    12: [0, 1, 2, 3],
    13: [0, 1, 2, 3, 4],
    14: [0, 1, 2, 3, 4, 5],
    15: [0, 1, 2, 3, 4, 5, 6],
}

# ─── Épaisseur des lignes flow (outline, main) ───────────────────────────────
LINE_WIDTH = {
    "Motorway":           (9, 7),
    "International road": (7, 7),
    "Major road":         (7, 6),
    "Secondary road":     (6, 6),
    "Connecting road":    (6, 5),
    "Major local road":   (5, 5),
    "Local road":         (5, 4),
    "Minor local road":   (4, 4),
    "Non public road":    (3, 4),
    "Parking road":       (3, 3),
}
DEFAULT_WIDTH = (7, 6)

# ─── Couleurs du trafic flow — palette relative0 TomTom ──────────────────────
# (outline_color, main_color) — RGBA
TRAFFIC_COLORS = {
    "closed":    ((0, 0, 0, 150),       (124, 121, 121, 150)),
    "very_slow": ((135, 8, 12, 255),    (172, 12, 17, 255)),
    "slow":      ((210, 124, 16, 255),  (243, 142, 17, 255)),
    "moderate":  ((218, 195, 17, 255),  (247, 222, 34, 255)),
    "free":      ((117, 223, 31, 255),  (116, 245, 12, 255)),
}

# ─── Incidents — style visuel par catégorie ───────────────────────────────────
# Styles : "hatched_red", "hatched_grey", "solid", None (masqué)
#
# icon_category :
#   0=Unknown  1=Accident  2=Fog  3=DangerousConditions  4=Rain
#   5=Ice  6=Jam  7=LaneClosed  8=RoadClosed  9=RoadWorks
#   10=Wind  11=Flooding  13=Cluster  14=BrokenDownVehicle

INCIDENT_STYLE = {
    0:  "hatched_grey",   # Unknown
    1:  "hatched_grey",   # Accident
    2:  "hatched_grey",   # Fog
    3:  "hatched_grey",   # Dangerous Conditions
    4:  "hatched_grey",   # Rain
    5:  "hatched_grey",   # Ice
    6:  "solid",          # Jam
    7:  "hatched_grey",   # Lane Closed
    8:  "hatched_red",    # Road Closed
    9:  "hatched_grey",   # Road Works
    10: "hatched_grey",   # Wind
    11: "hatched_grey",   # Flooding
    13: "hatched_grey",   # Cluster
    14: "hatched_grey",   # Broken Down Vehicle
}

# ─── Incidents — priorité de dessin (haute = par-dessus) ──────────────────────
INCIDENT_PRIORITY = {
    0:  10,     # Unknown
    1:  50,     # Accident
    2:  20,     # Fog
    3:  20,     # Dangerous Conditions
    4:  20,     # Rain
    5:  25,     # Ice
    6:  60,     # Jam
    7:  55,     # Lane Closed
    8:  100,    # Road Closed
    9:  30,     # Road Works
    10: 20,     # Wind
    11: 25,     # Flooding
    13: 40,     # Cluster
    14: 45,     # Broken Down Vehicle
}

# ─── Incidents — couleurs des tubes pleins (solid) par magnitude ──────────────
INCIDENT_MAGNITUDE_COLORS = {
    0: ((140, 60, 60, 255),  (200, 100, 100, 255)),   # Unknown
    1: ((170, 60, 20, 255),  (220, 120, 60, 255)),    # Minor
    2: ((160, 20, 10, 255),  (210, 50, 30, 255)),     # Moderate
    3: ((120, 5, 5, 255),    (170, 10, 10, 255)),     # Major
    4: ((100, 10, 10, 255),  (150, 15, 15, 255)),     # Indefinite
}

# ─── Incidents — couleurs des tubes hachurés ──────────────────────────────────
HATCHED_RED_COLORS  = ((190, 30, 30, 255), (216, 216, 216, 255))
HATCHED_GREY_COLORS = ((122, 128, 144, 255), (224, 224, 224, 255))

# ─── Incidents — épaisseur par type de route ──────────────────────────────────
INCIDENT_WIDTH = {
    "Motorway":           (9, 8),
    "International road": (8, 8),
    "Major road":         (8, 7),
    "Secondary road":     (7, 7),
    "Connecting road":    (7, 6),
    "Major local road":   (6, 6),
    "Local road":         (6, 5),
    "Minor local road":   (5, 5),
}
INCIDENT_DEFAULT_WIDTH = (8, 7)

# ─── Décalage directionnel ────────────────────────────────────────────────────
FLOW_OFFSET      = 0.5     # Décalage flow (× outline_w)
FLOW_VIS_OUTLINE = 0.55    # Bordure visible flow (× outline_w)
FLOW_VIS_MAIN    = 0.5     # Couleur visible flow (× main_w)

INCIDENT_OFFSET      = 0.35
INCIDENT_VIS_OUTLINE = 0.65
INCIDENT_VIS_MAIN    = 0.6

# ─── Activation incidents ─────────────────────────────────────────────────────
INCIDENTS_ENABLED = os.environ.get("INCIDENTS_ENABLED", "true").lower() != "false"

# ─── Budget API ───────────────────────────────────────────────────────────────
DAILY_QUOTA    = 50_000   # Requêtes gratuites TomTom par jour
CYCLES_PER_RUN = 36       # 6h / 10 min
RUNS_PER_DAY   = 4        # cron toutes les 6h

# ═══════════════════════════════════════════════════════════════════════════════
# ANNOTATIONS — Badges de délai sur les incidents
# ═══════════════════════════════════════════════════════════════════════════════
#
# Quand une zone a "annotations": True, les incidents listés ci-dessous
# affichent un badge avec le temps perdu (ex: "+12min", "+1h05").
#
# Pour activer l'annotation d'un type d'incident :
#   mettre True en face de son icon_category.
#
# Le badge n'apparaît que si le delay est ≥ ANNOTATION_MIN_DELAY_SEC.

INCIDENT_ANNOTATIONS = {
    0:  False,   # Unknown
    1:  False,   # Accident          ← mettre True pour annoter les accidents
    2:  False,   # Fog
    3:  False,   # Dangerous Conditions
    4:  False,   # Rain
    5:  False,   # Ice
    6:  True,    # Jam               ← delay affiché par défaut
    7:  False,   # Lane Closed
    8:  False,   # Road Closed
    9:  False,   # Road Works
    10: False,   # Wind
    11: False,   # Flooding
    13: False,   # Cluster
    14: False,   # Broken Down Vehicle
}

# ─── Paramètres visuels des badges ────────────────────────────────────────────

# Délai minimum pour afficher un badge (en secondes)
# Les incidents avec delay < ce seuil sont ignorés (trop mineur)
ANNOTATION_MIN_DELAY_SEC = 60       # 1 minute minimum

# Taille de la police (en pixels, à la résolution viewport 1920×1080)
ANNOTATION_FONT_SIZE = 13

# Padding intérieur du badge (horizontal, vertical) en pixels
ANNOTATION_PADDING = (6, 3)

# Rayon des coins arrondis du badge
ANNOTATION_CORNER_RADIUS = 4

# Couleur du texte dans les badges
ANNOTATION_TEXT_COLOR = (255, 255, 255, 255)    # Blanc

# Couleur de la bordure du badge (contour fin pour la lisibilité)
ANNOTATION_BORDER_COLOR = (0, 0, 0, 180)        # Noir semi-transparent

# Épaisseur de la bordure du badge
ANNOTATION_BORDER_WIDTH = 1

# Couleurs de fond des badges par magnitude — (background_rgba)
# Reprend la logique des couleurs d'incident mais adapté pour la lisibilité
ANNOTATION_BADGE_COLORS = {
    0: (180, 90, 90, 230),     # Unknown — rouge clair
    1: (210, 120, 40, 230),    # Minor — orange
    2: (200, 50, 25, 230),     # Moderate — rouge
    3: (150, 15, 15, 230),     # Major — rouge foncé
    4: (110, 10, 10, 230),     # Indefinite — rouge très foncé
}

# Couleur de badge par défaut (pour les incidents non-JAM, ex: accidents)
# Utilisée quand l'incident n'a pas de magnitude ou n'est pas un JAM
ANNOTATION_DEFAULT_BADGE_COLOR = (100, 100, 120, 230)   # Gris-bleu

# Distance minimale entre deux badges (en pixels) — anti-chevauchement
# Si un badge serait trop proche d'un existant, il est masqué
ANNOTATION_MIN_DISTANCE = 60

# ─── Positionnement bulle de dialogue ─────────────────────────────────────────
# Le badge est placé à l'ARRIÈRE de l'incident (là où les voitures s'arrêtent),
# décalé vers la DROITE du sens de circulation pour ne pas couvrir le flow.
# Un petit cône (bulle de dialogue) relie le badge au point d'ancrage.

# Distance entre le centre de la route et le centre du badge (en pixels viewport)
ANNOTATION_OFFSET_DISTANCE = 30

# Largeur du cône à sa base (côté badge), en pixels
ANNOTATION_CONE_WIDTH = 12

# Couleur du cône (même que la bordure du badge pour la continuité visuelle)
ANNOTATION_CONE_COLOR = (0, 0, 0, 180)
