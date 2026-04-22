"""
simulate_zone.py — Simulateur de coût API pour une zone TomTom
================================================================

Permet d'estimer le coût en requêtes API d'une nouvelle zone AVANT de
l'ajouter à config.py. Aucun appel réseau — calcul 100% local basé sur
la projection Web Mercator standard et les paramètres de config.py.

TROIS MODES D'UTILISATION :

  1. Édition directe du script (recommandé pour tests répétés) :
       Modifier la section URLS_TO_TEST ci-dessous, puis bouton ▶
       dans VSCode (ou `python simulate_zone.py` sans argument).

       → Accepte une LISTE (labels auto) :
           URLS_TO_TEST = ["url1", "url2", ...]

       → Ou un DICT (labels personnalisés) :
           URLS_TO_TEST = {"Ma zone": "url1", "Variante ouest": "url2"}

  2. Arguments en ligne de commande (ponctuel) :
       python simulate_zone.py "https://plan.tomtom.com/en/?p=46.2,7.3,12z"
       python simulate_zone.py "url1" "url2" "url3"

  3. Mode interactif (si URLS_TO_TEST vide et aucun argument) :
       Le script demande l'URL au clavier.

Priorité : arguments CLI > URLS_TO_TEST > prompt interactif
"""

# ═════════════════════════════════════════════════════════════════════════════
# ZONES À TESTER — édite cette section puis lance le script
# ═════════════════════════════════════════════════════════════════════════════
#
# Laisse la collection vide ({} ou []) pour utiliser les arguments CLI ou
# le mode interactif. Tu peux mélanger les styles selon ton envie du moment.

URLS_TO_TEST = {
    # Exemples (à remplacer par tes URL à tester) :
    "Landquart-Chur actuel":    "https://plan.tomtom.com/en/?p=46.91500,9.52654,12z",
    "Sargans-Landquart":        "https://plan.tomtom.com/en/?p=47.01,9.5,12z",
    "Chur-Isal-T":              "https://plan.tomtom.com/en/?p=46.81697,9.5,12z",
    "Monitoring_2026":          "https://plan.tomtom.com/en/?p=46.89357,9.49312,10z",
    "Globale":                  "https://plan.tomtom.com/en/?p=46.58894,9.10568,8z",
    "Amsteg_Göschenen":         "https://plan.tomtom.com/en/?p=46.72234,8.63188,12z",
    "Erstfeld-Amsteg":          "https://plan.tomtom.com/en/?p=46.82056,8.66716,12z",
    "Wassen-Göschenen":         "https://plan.tomtom.com/en/?p=46.68434,8.60391,13z",
    "GST_Nord":                 "https://plan.tomtom.com/en/?p=46.82968,8.60058,10z",
    "GSt_Sud":                  "https://plan.tomtom.com/en/?p=46.45552,8.76656,11z",
}

# ═════════════════════════════════════════════════════════════════════════════
# À partir d'ici, ne rien modifier (logique du simulateur)
# ═════════════════════════════════════════════════════════════════════════════

import sys
from pathlib import Path

# Import des fonctions existantes depuis capture.py et config.py
sys.path.insert(0, str(Path(__file__).parent))
from capture import _count_tiles_for_zone, parse_zone_url, resolve_road_types
from config import (
    ZONES, CYCLES_PER_RUN, RUNS_PER_DAY, DAILY_QUOTA,
    ROAD_TYPES_BY_ZOOM,
)

# ─── Tarif TomTom (pay-as-you-grow) ──────────────────────────────────────────
# Source : https://developer.tomtom.com/pricing (Traffic Flow/Incident tiles)
PRICE_PER_1000_USD = 0.08


def estimate_zone(url, label=None):
    """
    Calcule l'estimation complète pour une URL donnée.
    Retourne un dict avec toutes les métriques.
    """
    lat, lon, zoom = parse_zone_url(url)
    n_tiles = _count_tiles_for_zone(lat, lon, zoom)

    # Requêtes par cycle (flow + incidents, 2 couches par tuile)
    per_cycle = n_tiles * 2

    # Requêtes par run : cycles × couches + 1 base map
    per_run = per_cycle * CYCLES_PER_RUN + 1

    # Requêtes par jour : runs × (cycles + base)
    per_day = per_run * RUNS_PER_DAY

    # Requêtes par mois (base 31 jours pour pire cas)
    per_month = per_day * 31

    # roadTypes par défaut pour ce zoom (fallback)
    default_rt = ROAD_TYPES_BY_ZOOM.get(zoom, [0, 1, 2, 3])

    return {
        "label": label or f"Zone @ {lat},{lon}",
        "url": url,
        "lat": lat,
        "lon": lon,
        "zoom": zoom,
        "n_tiles": n_tiles,
        "per_cycle": per_cycle,
        "per_run": per_run,
        "per_day": per_day,
        "per_month": per_month,
        "default_road_types": default_rt,
    }


def current_total_daily():
    """Calcule la consommation journalière actuelle (zones de config.py)."""
    total = 0
    for name, zone_config in ZONES.items():
        lat, lon, zoom = parse_zone_url(zone_config["url"])
        n_tiles = _count_tiles_for_zone(lat, lon, zoom)
        per_cycle = n_tiles * 2
        per_day = per_cycle * CYCLES_PER_RUN * RUNS_PER_DAY + RUNS_PER_DAY
        total += per_day
    return total


def print_single_report(est, current_daily):
    """Affiche le rapport détaillé pour une zone."""
    print()
    print("═" * 70)
    print(f"📍 {est['label']}")
    print("═" * 70)
    print(f"  URL         : {est['url']}")
    print(f"  Coordonnées : lat={est['lat']}, lon={est['lon']}, zoom={est['zoom']}")
    print(f"  roadTypes par défaut au zoom {est['zoom']} : {est['default_road_types']}")
    print()
    print(f"  📐 Grille de tuiles")
    print(f"     {est['n_tiles']} tuiles × 2 couches (flow + incidents)")
    print()
    print(f"  📊 Consommation estimée")
    print(f"     Par cycle (10 min)   : {est['per_cycle']:>6} requêtes")
    print(f"     Par run (5h45)       : {est['per_run']:>6} requêtes")
    print(f"     Par jour ({RUNS_PER_DAY} runs)     : {est['per_day']:>6} requêtes")
    print(f"     Par mois (31 jours)  : {est['per_month']:>6} requêtes")
    print()

    # Impact sur le quota
    new_daily = current_daily + est["per_day"]
    pct_current = current_daily * 100 / DAILY_QUOTA
    pct_new = new_daily * 100 / DAILY_QUOTA

    print(f"  🎯 Impact sur le quota gratuit journalier ({DAILY_QUOTA:,} req)")
    print(f"     Avant ajout          : {current_daily:>6,} req/j ({pct_current:.1f}%)")
    print(f"     Après ajout          : {new_daily:>6,} req/j ({pct_new:.1f}%)")

    if new_daily <= DAILY_QUOTA:
        margin = DAILY_QUOTA - new_daily
        print(f"     ✅ Dans le quota gratuit — marge : {margin:,} req/j")
        print(f"     💰 Coût estimé : 0 $/mois")
    else:
        overage_per_day = new_daily - DAILY_QUOTA
        overage_per_month = overage_per_day * 31
        cost_usd = overage_per_month / 1000 * PRICE_PER_1000_USD
        print(f"     ⚠ Dépassement : {overage_per_day:,} req/j au-dessus du quota")
        print(f"     💰 Coût estimé : {cost_usd:.2f} $/mois "
              f"(tarif {PRICE_PER_1000_USD} $/1000 req)")

    print()


def print_comparison(estimations):
    """Affiche un tableau comparatif pour plusieurs zones testées."""
    if len(estimations) < 2:
        return
    print()
    print("═" * 70)
    print(f"📊 Comparaison des {len(estimations)} zones testées (tri par tuiles)")
    print("═" * 70)
    print(f"{'Zone':<32} {'Zoom':>5} {'Tuiles':>7} {'Req/jour':>10}")
    print("─" * 70)

    # Trier par nombre de tuiles pour identifier rapidement le meilleur
    sorted_est = sorted(estimations, key=lambda e: e["n_tiles"])
    best_tiles = sorted_est[0]["n_tiles"] if sorted_est else None

    for est in sorted_est:
        # Marquer la meilleure option (moins de tuiles)
        marker = " 🏆" if est["n_tiles"] == best_tiles and len(sorted_est) > 1 else "   "
        label = est["label"][:29] + ".." if len(est["label"]) > 31 else est["label"]
        print(f"{label:<32} {est['zoom']:>5} {est['n_tiles']:>7} "
              f"{est['per_day']:>10,}{marker}")
    print()


def resolve_sources():
    """
    Détermine la source des URLs à tester selon la priorité :
      1. Arguments CLI (si présents)
      2. URLS_TO_TEST dans le script (si non vide)
      3. Prompt interactif (fallback)

    Retourne une liste de tuples (label, url).
    """
    cli_args = sys.argv[1:]

    # Mode 1 : arguments CLI
    if cli_args:
        return [(f"Candidate #{i}" if len(cli_args) > 1 else "Candidate", url)
                for i, url in enumerate(cli_args, 1)]

    # Mode 2 : URLS_TO_TEST défini dans le script
    if URLS_TO_TEST:
        if isinstance(URLS_TO_TEST, dict):
            # Dict : labels personnalisés
            return list(URLS_TO_TEST.items())
        elif isinstance(URLS_TO_TEST, (list, tuple)):
            # Liste : labels auto-générés
            return [(f"Candidate #{i}", url)
                    for i, url in enumerate(URLS_TO_TEST, 1)]
        else:
            print(f"✗ URLS_TO_TEST doit être une liste ou un dict, "
                  f"pas {type(URLS_TO_TEST).__name__}")
            sys.exit(1)

    # Mode 3 : prompt interactif
    print("═" * 70)
    print("🧮 Simulateur de coût API TomTom — mode interactif")
    print("═" * 70)
    print("Colle l'URL plan.tomtom.com de la zone à tester.")
    print("Exemple : https://plan.tomtom.com/en/?p=46.22,7.35,12z")
    print()
    url = input("URL : ").strip()
    if not url:
        print("✗ Aucune URL fournie — abandon.")
        sys.exit(1)
    return [("Candidate", url)]


def main():
    sources = resolve_sources()

    # Contexte actuel (zones déjà configurées)
    current_daily = current_total_daily()
    print()
    print("─" * 70)
    print(f"📌 Configuration actuelle : {len(ZONES)} zones → "
          f"{current_daily:,} req/jour "
          f"({current_daily*100/DAILY_QUOTA:.1f}% du quota gratuit)")
    print("─" * 70)

    # Estimer chaque URL
    estimations = []
    for label, url in sources:
        try:
            est = estimate_zone(url, label=label)
            estimations.append(est)
            print_single_report(est, current_daily)
        except Exception as e:
            print(f"\n✗ Erreur sur « {label} » ({url}) : {e}\n")

    # Tableau comparatif si plusieurs zones
    print_comparison(estimations)


if __name__ == "__main__":
    main()