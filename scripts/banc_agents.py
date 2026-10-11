#!/usr/bin/env python3
"""Banc d'essai des agents du Bureau IA PixelKréol — une commande, à taper dans le terminal Hostinger :

    cd /opt/pixelkreol-bureau-ia && python3 scripts/banc_agents.py

Il pose ~200 questions fictives aux 25 agents (modèle = id de l'agent) via l'orchestrateur local (127.0.0.1:4001), note chaque réponse par
des règles automatiques (prix exacts, feux, renvois, refus, format…) et affiche le score par agent. Bibliothèque standard seulement.
Voir scripts/banc/LISEZMOI.md pour les options (--rapide, --agent, --sauver, --comparer, --juge, --routage, --detail).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from banc import cas as cas_mod  # noqa: E402
from banc import runner  # noqa: E402


def lire_env(*dossiers: Path) -> dict:
    for d in dossiers:
        f = d / ".env"
        if f.is_file():
            sortie = {}
            for ligne in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", ligne.strip())
                if m:
                    sortie[m.group(1)] = m.group(2).strip().strip('"').strip("'")
            return sortie
    return {}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("PK_ORCH_URL", "http://127.0.0.1:4001"), help="adresse de l'orchestrateur")
    p.add_argument("--agent", help="un ou plusieurs ids d'agents, séparés par des virgules (ex. devis,commercial)")
    p.add_argument("--rapide", action="store_true", help="3 questions par agent (≈ 75 appels)")
    p.add_argument("--pause", type=float, default=1.0, help="secondes entre deux questions (fournisseurs gratuits : ne pas descendre sous 1)")
    p.add_argument("--delai", type=float, default=180.0, help="délai maximal d'une réponse, en secondes")
    p.add_argument("--sauver", metavar="FICHIER", help="enregistre réponses et notes (JSON) pour les comparer plus tard")
    p.add_argument("--comparer", metavar="FICHIER", help="compare avec un passage enregistré (avant/après)")
    p.add_argument("--detail", action="store_true", help="liste les vérifications ratées, cas par cas")
    p.add_argument("--juge", action="store_true", help="ajoute l'avis d'un modèle (pk-costaud via LiteLLM) : NON déterministe, en plus des règles")
    p.add_argument("--juge-url", default=os.environ.get("PK_LITELLM_URL", "http://127.0.0.1:4000"))
    p.add_argument("--routage", action="store_true", help="vérifie aussi que pk-auto choisit le bon agent pour chaque question (/route)")
    p.add_argument("--liste", action="store_true", help="affiche les cas sans appeler l'orchestrateur")
    a = p.parse_args(argv)

    liste = cas_mod.tous()
    if a.agent:
        voulus = {x.strip() for x in a.agent.split(",") if x.strip()}
        inconnus = voulus - {c.agent for c in liste}
        if inconnus:
            print("Agent(s) inconnu(s) : " + ", ".join(sorted(inconnus)), file=sys.stderr)
            return 2
        liste = [c for c in liste if c.agent in voulus]
    if a.rapide:
        vus: dict[str, int] = {}
        liste = [c for c in liste if vus.__setitem__(c.agent, vus.get(c.agent, 0) + 1) is None and vus[c.agent] <= 3]
    if a.liste:
        for c in liste:
            print(f"{c.cle:<36} {c.question[:100]}")
        print(f"{len(liste)} cas")
        return 0

    racine = Path(__file__).resolve().parent.parent
    env = lire_env(Path.cwd(), racine)
    cle = os.environ.get("PK_API_KEY") or env.get("PK_API_KEY") or os.environ.get("LITELLM_MASTER_KEY") or env.get("LITELLM_MASTER_KEY") or ""
    if not cle:
        print("Clé introuvable : lancer la commande depuis le dossier du Bureau IA (fichier .env) ou définir PK_API_KEY.", file=sys.stderr)
        return 2
    juge = None
    if a.juge:
        cle_llm = os.environ.get("LITELLM_MASTER_KEY") or env.get("LITELLM_MASTER_KEY") or cle
        juge = {"url": a.juge_url, "cle": cle_llm, "modele": "pk-costaud"}
    avant = runner.charger(a.comparer) if a.comparer else None
    print(f"Banc d'essai : {len(liste)} questions, {len({c.agent for c in liste})} agents, orchestrateur {a.url}"
          f" (≈ {len(liste) * (a.pause + 8) / 60:.0f} min).  Règles = automatique" + ("  ·  juge IA = non déterministe" if juge else ""))
    t0 = time.monotonic()
    try:
        res = runner.lancer(liste, a.url, cle, pause=a.pause, delai=a.delai, juge=juge, routage=a.routage)
    except KeyboardInterrupt:
        print("\nInterrompu.")
        return 130
    print(f"\nTerminé en {(time.monotonic() - t0) / 60:.1f} min.")
    runner.afficher(res, avant, a.detail)
    if a.sauver:
        runner.sauver(res, a.sauver, {"date": time.strftime("%Y-%m-%d %H:%M"), "url": a.url, "juge": bool(juge), "rapide": a.rapide})
        print(f"\nRésultats enregistrés dans {a.sauver}")
    return 0 if all(not r.erreur for r in res) else 1


if __name__ == "__main__":
    sys.exit(main())
