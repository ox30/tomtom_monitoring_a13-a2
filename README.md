# Captures TomTom — Trafic A13/A2

Captures automatiques (flow vectoriel + incidents) toutes les ~10 minutes.
Fuseau horaire : **Europe/Zurich** (CET/CEST).

## Télécharger

| Quoi | Comment |
|---|---|
| **Une capture** | Naviguer dans les dossiers ci-dessous → clic sur l'image → bouton ↓ **Download** |
| **Tout télécharger (ZIP)** | [📦 Télécharger le ZIP](https://github.com/ox30/tomtom_monitoring_a13-a2/archive/refs/heads/captures.zip) |

## Structure

```
captures/                                    ← composites toutes les 10 min
  2026-03-29/
    zone_A13_Chur/
      2026-03-29-0830_zone_A13_Chur.jpg      ← heure locale Zurich
      ...
    zone_Chur_Isla-T/
      ...
    zone_globale_A2_A13/
      ...

bases/                                       ← cartes de fond (1× par run)
  2026-03-29/
    zone_A13_Chur/
      2026-03-29-0600_zone_A13_Chur_base.jpg
    ...
```

## Zones capturées

| Zone | Couverture |
|---|---|
| `zone_globale_A2_A13` | Vue d'ensemble A2–A13 |
| `zone_A13_Chur` | Secteur A13 autour de Chur |
| `zone_Chur_Isla-T` | Détail Chur – Isla/Thusis |

## Rétention

Les captures et bases de plus de **7 jours** sont automatiquement supprimées.
