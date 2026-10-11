"""Mesures HORS LIGNE des fiches d'agents (aucun modèle, aucun fournisseur) : routage de pk-auto sur les questions du banc + audit de complétude des fiches.

Usage : python tests/mesure_agents.py [--agents DOSSIER] [--detail]
        (--agents : dossier agents/ à mesurer ; par défaut celui du dépôt ; pour « avant » : git show REF:agents/... dans un dossier temporaire)
Le score de QUALITÉ des réponses se mesure avec scripts/banc_agents.py contre l'orchestrateur du serveur.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

import httpx

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "tests"))
sys.path.insert(0, str(RACINE / "scripts"))
from banc import audit, cas  # noqa: E402
from conftest import FauxLiteLLM, charger_app  # noqa: E402


class MP:
    def setenv(self, k, v):
        import os
        os.environ[k] = v

    def delenv(self, k, raising=False):
        import os
        os.environ.pop(k, None)


def routage(dossier: Path) -> dict[str, tuple[int, int]]:
    """{agent attendu: (bien routées, total)} avec l'IA de routage en panne (mots-clés seuls : cas le plus défavorable)."""
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(dossier, tmp / "agents")
    app = charger_app(MP(), tmp / "agents")
    faux = FauxLiteLLM()
    faux.par_defaut = lambda corps: FauxLiteLLM.statut(429, "quota")
    client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
    res: dict[str, list[int]] = {}
    erreurs = []

    async def tout():
        for c in cas.tous():
            if not c.route:
                continue
            agent, _ = await app.choisir_agent(client, [{"role": "user", "content": c.question}])
            r = res.setdefault(c.route, [0, 0])
            r[1] += 1
            r[0] += agent["id"] == c.route
            if agent["id"] != c.route:
                erreurs.append((c.cle, agent["id"]))
    asyncio.run(tout())
    shutil.rmtree(tmp, ignore_errors=True)
    routage.erreurs = erreurs
    return {k: tuple(v) for k, v in res.items()}


def main() -> int:
    detail = "--detail" in sys.argv
    dossier = Path(sys.argv[sys.argv.index("--agents") + 1]) if "--agents" in sys.argv else RACINE / "agents"
    r = routage(dossier)
    ok, tot = sum(v[0] for v in r.values()), sum(v[1] for v in r.values())
    a = audit.auditer(dossier)
    print(f"Fiches mesurées : {dossier}")
    print(f"\n{'agent':<24}{'routage pk-auto':>16}{'fiche (10 critères)':>22}{'taille':>8}")
    for ag in a:
        v = r.get(ag, (0, 0))
        print(f"{ag:<24}{(str(v[0]) + '/' + str(v[1])) if v[1] else '—':>16}{a[ag]['score'] * 100:>21.0f}%{a[ag]['taille']:>8}")
    moy = sum(x["score"] for x in a.values()) / len(a)
    print(f"{'GLOBAL':<24}{ok:>10}/{tot:<5}{moy * 100:>21.0f}%{sum(x['taille'] for x in a.values()):>8}")
    if detail:
        print("\nRoutages ratés (cas → agent choisi) :")
        for cle, choisi in routage.erreurs:
            print(f"  {cle} → {choisi}")
        print("\nCritères manquants par agent :")
        for ag, x in a.items():
            print(f"  {ag}: " + "; ".join(x["manque"]) if x["manque"] else f"  {ag}: complet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
