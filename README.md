# TomTom Traffic Capture — Vector Flow Edition

Capture automatique du trafic routier (A13/A2, Grisons/Gothard) via l'**API TomTom vectorielle**, orchestrée par GitHub Actions toutes les 10 minutes.

## Architecture

Le système superpose 3 couches via Pillow pour produire une image composite :

1. **Carte de base** — API Static Image TomTom (1 requête HTTP, pas de navigateur)
2. **Traffic Flow** — Vector Flow Tiles (`.pbf`) avec filtrage par type de route et rendu fidèle à plan.tomtom.com
3. **Incidents** — Vector Incident Tiles (`.pbf`) avec 3 styles visuels : tube hachuré rouge (fermetures), tube hachuré gris (travaux/météo), tube plein coloré (bouchons)

Chaque couche est dessinée avec un **décalage directionnel** : le tube est poussé vers la droite du sens de circulation, ce qui permet de distinguer les deux directions même en vue dézoomée.

Le rendu utilise un **supersampling** : les couches vectorielles sont dessinées à 2× la résolution finale, puis réduites avec un filtre LANCZOS. Cela produit des lignes anti-aliasées (lissées) sans aucune requête API supplémentaire.

## Workflow

Le workflow GitHub Actions fonctionne en boucle continue :

1. Au démarrage du run : **sauvegarde des cartes de base** (`python capture.py --save-bases`) → archivées dans `bases/`
2. Boucle toutes les 10 minutes : **capture flow + incidents** (`python capture.py`) → archivées dans `captures/`
3. Le run dure ~5h45, puis le cron relance automatiquement

Les paramètres sont ajustables au lancement manuel (**Actions → Run workflow**) :

| Paramètre | Défaut | Description |
|---|---|---|
| **Intervalle** | `600` | Secondes entre chaque capture (texte libre) |
| **Cycles** | `0` | Nombre de captures (0 = boucle jusqu'à 5h45) |
| **Incidents** | `true` | Afficher les incidents (`true`/`false`) |
| **Style fond de carte** | `light` | Style de la carte de base (`light`/`main`/`night`) |

## Stockage (branche `captures`)

```
captures/                                        ← composites toutes les 10 min
  2026-03-29/
    zone_A13_Chur/
      2026-03-29-0830_zone_A13_Chur.jpg          ← heure locale Europe/Zurich
      2026-03-29-0840_zone_A13_Chur.jpg
      ...
    zone_Chur_Isla-T/
      ...

bases/                                           ← cartes de fond (1× par run)
  2026-03-29/
    zone_A13_Chur/
      2026-03-29-0600_zone_A13_Chur_base.jpg
    ...
```

Rétention automatique : **7 jours**. Les dossiers plus anciens sont supprimés à chaque exécution.

**Poids indicatif** (4 zones, `SUPERSAMPLE=2`, `OUTPUT_QUALITY=95`) : ~1.45 MB/cycle → ~204 MB/jour → ~1.4 GB sur 7 jours. Pour rester sous la recommandation GitHub de 1 GB, réduire la rétention à 3 jours ou diminuer `OUTPUT_QUALITY`.

## Configuration

Tout le paramétrage se fait dans `capture.py`, section `# ─── Configuration`. Rien à changer dans le workflow.

### Zones de capture

Collez directement l'URL de plan.tomtom.com. Le zoom fractionnaire est arrondi automatiquement.

```python
ZONES = {
    "zone_globale_A2_A13": "https://plan.tomtom.com/en/?p=46.68973,8.93561,8.55z",
    "zone_A13_Chur":       "https://plan.tomtom.com/en/?p=46.89942,9.32459,9.75z",
    "zone_Chur_Isla-T":    "https://plan.tomtom.com/en/?p=46.84086,9.45618,12.17z",
    "zone_GST":            "https://plan.tomtom.com/en/?p=46.6353,8.68195,10.3z",
}
```

Pour ajouter une zone : naviguer sur [plan.tomtom.com](https://plan.tomtom.com/), zoomer à la vue souhaitée, copier l'URL, ajouter une ligne dans `ZONES`.

### Qualité de rendu

```python
# Supersampling — rendu interne à N× la résolution, downscale LANCZOS final.
# Produit des lignes anti-aliasées sans surcoût API. Aucun impact sur le quota TomTom.
# 1 = désactivé (comportement original)
# 2 = recommandé — bon compromis qualité/RAM (~600 MB pic)
# 3 = meilleur rendu, RAM ~3× plus élevée
SUPERSAMPLE = 2

# Qualité JPEG de sortie.
# 88 = valeur d'origine (fichiers plus légers, légère compression visible sur les traits)
# 95 = recommandé — réduit les artefacts de compression autour des tubes flow/incidents
OUTPUT_QUALITY = 95
```

> **Note :** augmenter `SUPERSAMPLE` ou `OUTPUT_QUALITY` améliore la qualité visuelle mais augmente le poids des fichiers et donc la consommation de stockage GitHub.

### Style de la carte de base

Trois modes disponibles, sélectionnables au lancement du workflow ou dans le fichier :

| Style | Rendu | Utilisation |
|-------|-------|-------------|
| `"light"` | Fond gris clair désaturé | Monitoring trafic (défaut) — le flow et les incidents ressortent clairement |
| `"main"` | Couleurs standard TomTom | Vue classique avec toutes les couleurs de la carte |
| `"night"` | Mode sombre TomTom | Affichage en salle de contrôle, faible luminosité |

Le mode "light" télécharge la carte `main` puis applique une transformation Pillow (désaturation + éclaircissement). Les paramètres sont ajustables :

```python
BASE_MAP_STYLE         = os.environ.get("BASE_MAP_STYLE", "light")
LIGHT_SATURATION       = 0.05    # 0.0 = gris pur, 1.0 = couleurs originales
LIGHT_BRIGHTNESS       = 1.1     # > 1.0 = plus clair
LIGHT_CONTRAST         = 1.5     # > 1.0 = plus contrasté
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

| Code | Type | Exemple |
|------|------|---------|
| 0 | Motorway | A2, A13 |
| 1 | International road | Routes nationales |
| 2 | Major road | Routes cantonales |
| 3 | Secondary road | Routes régionales |
| 4 | Connecting road | Liaisons locales |
| 5 | Major local road | Routes communales principales |
| 6 | Local road | Routes communales |
| 7 | Minor local road | Chemins |

> **Note :** le filtrage s'applique uniquement au flow. Les incidents sont affichés sur toutes les routes quel que soit le zoom (une fermeture de col reste critique même sur une petite route).

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

> **Astuce supersampling :** avec `SUPERSAMPLE=2`, le downscale LANCZOS affine légèrement les traits. Si les tubes paraissent trop fins, augmenter les valeurs d'un cran (ex. Motorway `(7, 6)`) compense cet effet.

### Couleurs du trafic (flow)

Palette `relative0` de TomTom — `(outline_rgba, main_rgba)` :

```python
TRAFFIC_COLORS = {
    "closed":    ((0, 0, 0, 150),       (124, 121, 121, 150)),  # Gris semi-transparent
    "very_slow": ((135, 8, 12, 255),    (172, 12, 17, 255)),    # Rouge foncé (< 15%)
    "slow":      ((210, 124, 16, 255),  (243, 142, 17, 255)),   # Orange (15-35%)
    "moderate":  ((218, 195, 17, 255),  (247, 222, 34, 255)),   # Jaune (35-75%)
    "free":      ((117, 223, 31, 255),  (116, 245, 12, 255)),   # Vert (≥ 75%)
}
```

### Décalage directionnel

Le tube flow/incident est décalé vers la droite du sens de circulation pour séparer visuellement les deux directions. 6 multiplicateurs contrôlent ce comportement :

```python
# Flow
FLOW_OFFSET      = 0.5    # Distance du centre de la route (× outline_w)
FLOW_VIS_OUTLINE = 0.55   # Épaisseur bordure visible (× outline_w)
FLOW_VIS_MAIN    = 0.5    # Épaisseur couleur visible (× main_w)

# Incidents (moins décalé → dépasse légèrement le flow)
INCIDENT_OFFSET      = 0.35
INCIDENT_VIS_OUTLINE = 0.65
INCIDENT_VIS_MAIN    = 0.6
```

Pour séparer davantage les deux sens : augmenter `FLOW_OFFSET`. Pour un tube plus fin : baisser `VIS_OUTLINE` et `VIS_MAIN`.

### Incidents — style par catégorie

Chaque type d'incident est affecté à un style visuel. Modifiez `INCIDENT_STYLE` pour changer l'apparence :

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
    8:  "hatched_red",    # Road Closed  ← tube rouge/gris avec carrés
    9:  "hatched_grey",   # Road Works   ← tube gris-bleu/gris avec carrés
    10: "hatched_grey",   # Wind
    11: "hatched_grey",   # Flooding
    13: "hatched_grey",   # Cluster
    14: "hatched_grey",   # Broken Down Vehicle
}
```

Les styles visuels disponibles :

| Style | Rendu | Usage |
|-------|-------|-------|
| `"hatched_red"` | Contour rouge fin, fond gris clair, carrés rouges (1/3 carré, 2/3 gris) | Fermetures de route |
| `"hatched_grey"` | Contour gris-bleu fin, fond gris très clair, carrés gris-bleu | Travaux, météo, autres |
| `"solid"` | Tube plein, couleur selon magnitude (rouge foncé → orange) | Bouchons (Jam) |
| `None` | Pas affiché | Masquer une catégorie |

### Incidents — priorité de dessin

Les incidents de priorité haute sont dessinés par-dessus ceux de priorité basse :

```python
INCIDENT_PRIORITY = {
    8:  100,    # Road Closed — toujours visible par-dessus tout
    6:  60,     # Jam — par-dessus les hachurés
    7:  55,     # Lane Closed
    1:  50,     # Accident
    14: 45,     # Broken Down Vehicle
    13: 40,     # Cluster
    9:  30,     # Road Works
    5:  25,     # Ice
    11: 25,     # Flooding
    2:  20,     # Fog
    ...
}
```

### Incidents — couleurs des bouchons (solid)

Par magnitude, du plus léger au plus grave :

```python
INCIDENT_MAGNITUDE_COLORS = {
    0: ((140, 60, 60),  (200, 100, 100)),   # Unknown — rouge clair
    1: ((170, 60, 20),  (220, 120, 60)),    # Minor — orange
    2: ((160, 20, 10),  (210, 50, 30)),     # Moderate — rouge moyen
    3: ((120, 5, 5),    (170, 10, 10)),     # Major — rouge foncé
    4: ((100, 10, 10),  (150, 15, 15)),     # Indefinite — rouge très foncé
}
```

### Incidents — couleurs des tubes hachurés

```python
HATCHED_RED_COLORS  = ((190, 30, 30), (216, 216, 216))    # carrés rouges + fond gris clair
HATCHED_GREY_COLORS = ((122, 128, 144), (224, 224, 224))  # carrés gris-bleu + fond gris
```

### Épaisseur des incidents

Légèrement plus épais que le flow pour ressortir :

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

## Budget API

Le script affiche un rapport de consommation au début de chaque exécution :

```
📊 BUDGET API — Estimation de consommation
  Par cycle:       120 requêtes (4 zones)
  Par run (5h45):  4324 requêtes
  Par jour (×4):   17296 / 50000 = 34.6%
  Marge:           ~7 zones supplémentaires possibles
  🔍 Supersampling ×2 actif — qualité améliorée, zéro surcoût API
  ✓ Budget confortable
```

Le quota gratuit TomTom est de **50'000 requêtes/jour**. Un avertissement s'affiche au-delà de 70%. Le supersampling n'a **aucun impact** sur ce quota.

## Installation

1. Créer un compte sur [developer.tomtom.com](https://developer.tomtom.com/) → créer une application → copier l'**API Key**
2. Dans le repo GitHub : **Settings → Secrets and variables → Actions → New repository secret** avec le nom `TOMTOM_API_KEY`
3. Le workflow démarre automatiquement. Pour un lancement manuel : **Actions → Run workflow**

## Développement local

```bash
export TOMTOM_API_KEY="votre_clé"
export BASE_MAP_STYLE="light"          # ou "main" ou "night"
pip install requests Pillow
python capture.py              # un cycle flow + incidents
python capture.py --save-bases # sauvegarder les cartes de base
```

## Dépendances

Uniquement `requests` et `Pillow`. Le parseur protobuf pour les vector tiles est intégré directement dans `capture.py` — aucune dépendance externe.

## Captures

| | |
|---|---|
| **Naviguer** | [branche captures](../../tree/captures) |
| **Télécharger (ZIP)** | [captures.zip](../../archive/refs/heads/captures.zip) |
