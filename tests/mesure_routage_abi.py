"""Mesure le taux de bonne orientation de pk-auto pour l'instance ABI Holding (données fictives).

Usage : python tests/mesure_routage_abi.py [--detail]
Deux scénarios, sans jamais appeler un vrai fournisseur :
  A) IA de routage en panne (seuls les mots-clés décident) ;
  B) IA parfaite quand les mots-clés hésitent (borne haute).
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cas_routage_abi import CAS, CAS_CONTROLE  # noqa: E402
from conftest import FauxLiteLLM, charger_app  # noqa: E402
from mesure_routage import MP  # noqa: E402

AGENTS_ABI = Path(__file__).resolve().parent.parent / "instances" / "abi" / "agents"


def mesurer(oracle: bool, cas):
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(AGENTS_ABI, tmp / "agents")
    app = charger_app(MP(), tmp / "agents", PK_MARQUE="ABI Holding")
    faux = FauxLiteLLM()
    courant = {}
    faux.par_defaut = (lambda corps: FauxLiteLLM.ok(courant["id"])) if oracle else FauxLiteLLM.statut(429, "quota")
    client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
    res = []

    async def tout():
        for texte, attendu in cas:
            courant["id"] = attendu
            agent, methode = await app.choisir_agent(client, [{"role": "user", "content": texte}])
            res.append((texte, attendu, agent["id"], methode))
    asyncio.run(tout())
    shutil.rmtree(tmp, ignore_errors=True)
    return res


def main():
    detail = "--detail" in sys.argv
    for nom_jeu, jeu in (("jeu principal", CAS), ("jeu de contrôle (non utilisé pour le réglage)", CAS_CONTROLE)):
        print(f"\n=============== ABI — {nom_jeu} : {len(jeu)} demandes ===============")
        for titre, oracle in (("A) IA de routage en panne (mots-clés seuls)", False),
                              ("B) IA parfaite quand les mots-clés hésitent (borne haute)", True)):
            res = mesurer(oracle, jeu)
            bons = sum(1 for _, a, g, _ in res if a == g)
            print(f"\n{titre}\n  bien orientées : {bons}/{len(res)} = {100 * bons / len(res):.0f} %")
            if detail:
                for texte, a, g, m in res:
                    if a != g:
                        print(f"    ✘ [{m}] attendu {a:<16} obtenu {g:<16} « {texte[:80]} »")


if __name__ == "__main__":
    main()
