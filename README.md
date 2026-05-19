# TomTom Traffic Vector Capture

Projet personnel d'exploration technique autour de l'**API TomTom Traffic Vector Tiles**. L'objectif est d'apprendre à parser le format `protobuf` natif des vector tiles et à produire un rendu cartographique fidèle en Python pur (pas de navigateur, pas de SDK), orchestré par GitHub Actions.

## Architecture

Le système superpose 3 couches via Pillow pour produire une image composite :

1. **Carte de base** — API Static Image TomTom (1 requête HTTP, pas de navigateur)
2. **Traffic Flow** — Vector Flow Tiles (`.pbf`) avec filtrage par type de route et rendu fidèle à plan.tomtom.com
3. **Incidents** — Vector Incident Tiles (`.pbf`) avec 3 styles visuels : tube hachuré rouge (fermetures), tube hachuré gris (travaux/météo), tube plein coloré (bouchons)

Chaque couche est dessinée avec un **décalage directionnel** : le tube est poussé vers la droite du sens de circulation, ce qui permet de distinguer les deux directions même en vue dézoomée.

Le rendu utilise un **supersampling** : les couches vectorielles sont dessinées à 2× la résolution finale, puis réduites avec un filtre LANCZOS. Cela produit des lignes anti-aliasées (lissées) sans aucune requête API supplémentaire.

## Workflow

Le workflow GitHub Actions fonctionne en boucle continue :

1. Au démarrage du run : **sauvegarde des cartes de base** (`python capture.py --save-bases`)
2. Boucle régulière : **capture flow + incidents** (`python capture.py`)
3. Le run dure ~5h45, puis le cron relance automatiquement

Les paramètres sont ajustables au lancement manuel (**Actions → Run workflow**) :

| Paramètre | Défaut | Description |
|---|---|---|
| **Intervalle** | `600` | Secondes entre chaque capture |
| **Cycles** | `0` | Nombre de captures (0 = boucle jusqu'à 5h45) |
| **Incidents** | `true` | Afficher les incidents (`true`/`false`) |
| **Style fond de carte** | `night` | Style de la carte de base (`light`/`main`/`night`) |

## Configuration

Tout le paramétrage se fait dans `config.py`. Rien à changer dans le workflow.

### Zones de capture

Collez directement l'URL de [plan.tomtom.com](https://plan.tomtom.com/). Le zoom fractionnaire est arrondi automatiquement.

```python
ZONES = {
    "zone_a": "https://plan.tomtom.com/en/?p=<lat>,<lon>,<zoom>",
    "zone_b": "https://plan.tomtom.com/en/?p=<lat>,<lon>,<zoom>",
}
```

Pour ajouter une zone : naviguer sur plan.tomtom.com, zoomer à la vue souhaitée, copier l'URL, ajouter une ligne dans `ZONES`.

### Qualité de rendu

```python
# Supersampling — rendu interne à N× la résolution, downscale LANCZOS final.
# Produit des lignes anti-aliasées sans surcoût API.
# 1 = désactivé
# 2 = recommandé — bon compromis qualité/RAM (~600 MB pic)
# 3 = meilleur rendu, RAM ~3× plus élevée
SUPERSAMPLE = 2

# Qualité JPEG de sortie (0-100).
OUTPUT_QUALITY = 95
```

### Style de la carte de base

Trois modes disponibles, sélectionnables au lancement du workflow ou dans le fichier :

| Style | Rendu |
|-------|-------|
| `"light"` | Fond gris clair désaturé — flow et incidents ressortent clairement |
| `"main"` | Couleurs standard TomTom |
| `"night"` | Mode sombre TomTom |

Le mode `"light"` télécharge la carte `main` puis applique une transformation Pillow (désaturation + éclaircissement). Les paramètres sont ajustables :

```python
BASE_MAP_STYLE         = os.environ.get("BASE_MAP_STYLE", "night")
LIGHT_SATURATION       = 0.05    # 0.0 = gris pur, 1.0 = couleurs originales
LIGHT_BRIGHTNESS       = 1.1
LIGHT_CONTRAST         = 1.5
```

### Filtrage des routes par zoom

Détermine quels types de route affichent du flow à chaque niveau de zoom. Plus le zoom est faible, moins on affiche de routes secondaires pour garder la lisibilité.

```python
ROAD_TYPES_BY_ZOOM = {
    8:  [0],              # Motorway uniquement
    9:  [0, 1],           # + International
    10: [0, 1, 2],        # + Major
    11: [0, 1, 2, 3],     # + Secondary
    12: [0, 1, 2, 3],
    13: [0, 1, 2, 3, 4],  # + Connecting
    14: [0, 1, 2, 3, 4, 5],
    15: [0, 1, 2, 3, 4, 5, 6],
}
```

Les types de route TomTom :

| Code | Type |
|------|------|
| 0 | Motorway |
| 1 | International road |
| 2 | Major road |
| 3 | Secondary road |
| 4 | Connecting road |
| 5 | Major local road |
| 6 | Local road |
| 7 | Minor local road |

> Le filtrage s'applique uniquement au flow. Les incidents sont affichés sur toutes les routes quel que soit le zoom.

### Épaisseur des lignes (flow)

Chaque type de route a une épaisseur `(outline, main)` en pixels. L'outline est la bordure sombre, le main est la couleur vive.

```python
LINE_WIDTH = {
    "Motorway":           (5, 4),
    "International road": (4, 4),
    "Major road":         (4, 3),
    "Secondary road":     (3, 3),
    "Connecting road":    (3, 2),
    "Major local road":   (2, 2),
    "Local road":         (2, 1),
    "Minor local road":   (1, 1),
}
```

### Couleurs du trafic (flow)

Palette `relative0` de TomTom — `(outline_rgba, main_rgba)` :

```python
TRAFFIC_COLORS = {
    "closed":    ((0, 0, 0, 150),       (124, 121, 121, 150)),
    "very_slow": ((135, 8, 12, 255),    (172, 12, 17, 255)),
    "slow":      ((210, 124, 16, 255),  (243, 142, 17, 255)),
    "moderate":  ((218, 195, 17, 255),  (247, 222, 34, 255)),
    "free":      ((117, 223, 31, 255),  (116, 245, 12, 255)),
}
```

### Décalage directionnel

Le tube flow/incident est décalé vers la droite du sens de circulation pour séparer visuellement les deux directions. 6 multiplicateurs contrôlent ce comportement :

```python
# Flow
FLOW_OFFSET      = 0.5
FLOW_VIS_OUTLINE = 0.55
FLOW_VIS_MAIN    = 0.5

# Incidents (moins décalé → dépasse légèrement le flow)
INCIDENT_OFFSET      = 0.35
INCIDENT_VIS_OUTLINE = 0.65
INCIDENT_VIS_MAIN    = 0.6
```

### Incidents — style par catégorie

```python
INCIDENT_STYLE = {
    0:  "hatched_grey",   # Unknown
    1:  "hatched_grey",   # Accident
    2:  "hatched_grey",   # Fog
    3:  "hatched_grey",   # Dangerous Conditions
    4:  "hatched_grey",   # Rain
    5:  "hatched_grey",   # Ice
    6:  "solid",          # Jam          ← tube plein, couleur par magnitude
    7:  "hatched_grey",   # Lane Closed
    8:  "hatched_red",    # Road Closed
    9:  "hatched_grey",   # Road Works
    10: "hatched_grey",   # Wind
    11: "hatched_grey",   # Flooding
    13: "hatched_grey",   # Cluster
    14: "hatched_grey",   # Broken Down Vehicle
}
```

### Incidents — couleurs des bouchons (`solid`)

Par magnitude :

```python
INCIDENT_MAGNITUDE_COLORS = {
    0: ((140, 60, 60),  (200, 100, 100)),
    1: ((170, 60, 20),  (220, 120, 60)),
    2: ((160, 20, 10),  (210, 50, 30)),
    3: ((120, 5, 5),    (170, 10, 10)),
    4: ((100, 10, 10),  (150, 15, 15)),
}
```

### Incidents — couleurs des tubes hachurés

```python
HATCHED_RED_COLORS  = ((190, 30, 30), (216, 216, 216))
HATCHED_GREY_COLORS = ((122, 128, 144), (224, 224, 224))
```

### Épaisseur des incidents

```python
INCIDENT_WIDTH = {
    "Motorway":           (6, 5),
    "International road": (5, 5),
    "Major road":         (5, 4),
    "Secondary road":     (4, 4),
    "Connecting road":    (4, 3),
    "Major local road":   (3, 3),
    "Local road":         (3, 2),
    "Minor local road":   (2, 2),
}
```

## Installation

1. Créer un compte sur [developer.tomtom.com](https://developer.tomtom.com/) → créer une application → copier l'**API Key**
2. Dans le repo GitHub : **Settings → Secrets and variables → Actions → New repository secret** avec le nom `TOMTOM_API_KEY`
3. Pour lancement manuel : **Actions → Run workflow**

## Développement local

```bash
export TOMTOM_API_KEY="votre_clé"
export BASE_MAP_STYLE="night"
pip install requests Pillow
python capture.py              # un cycle flow + incidents
python capture.py --save-bases # sauvegarder les cartes de base
```

## Dépendances

Uniquement `requests` et `Pillow`. Le parseur protobuf pour les vector tiles est intégré directement dans `capture.py` — aucune dépendance externe.

## Licence

Projet personnel — utilisation à titre d'apprentissage. L'usage de l'API TomTom est soumis aux [TomTom Developer Terms and Conditions](https://developer.tomtom.com/terms-and-conditions).
