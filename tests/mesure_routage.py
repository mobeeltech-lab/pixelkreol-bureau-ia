"""Mesure le taux de bonnes réponses de pk-auto sur tests/cas_routage.py.

Usage : python tests/mesure_routage.py [--detail]

Deux scénarios, sans jamais appeler un vrai fournisseur :
  A) « IA de routage en panne » : seuls les mots-clés décident (cas le plus défavorable) ;
  B) « IA parfaite » : quand les mots-clés hésitent, l'IA répond le bon agent (borne haute).
     Les erreurs « sûres » (mots-clés trop confiants) ne sont jamais corrigées par l'IA.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cas_routage import CAS, CAS_CONTROLE  # noqa: E402
from conftest import FauxLiteLLM, charger_app  # noqa: E402


class MP:  # mini monkeypatch
    def __init__(self):
        import os
        self.os = os
    def setenv(self, k, v):
        self.os.environ[k] = v
    def delenv(self, k, raising=False):
        self.os.environ.pop(k, None)


def mesurer(oracle: bool, cas=None):
    cas = cas or CAS
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(Path(__file__).resolve().parent.parent / "agents", tmp / "agents")
    app = charger_app(MP(), tmp / "agents")
    faux = FauxLiteLLM()
    attendu_courant = {}

    def ia(corps):
        if not oracle:
            return FauxLiteLLM.statut(429, "quota")
        return FauxLiteLLM.ok(attendu_courant["id"])
    faux.par_defaut = ia
    client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
    res = []

    async def tout():
        for texte, attendu in cas:
            attendu_courant["id"] = attendu
            agent, methode = await app.choisir_agent(client, [{"role": "user", "content": texte}])
            res.append((texte, attendu, agent["id"], methode))
    asyncio.run(tout())
    shutil.rmtree(tmp, ignore_errors=True)
    return res


def main():
    detail = "--detail" in sys.argv
    for nom_jeu, jeu in (("jeu principal", CAS), ("jeu de contrôle (non utilisé pour le réglage)", CAS_CONTROLE)):
        print(f"\n=============== {nom_jeu} : {len(jeu)} demandes ===============")
        for titre, oracle in (("A) IA de routage en panne (mots-clés seuls)", False),
                              ("B) IA parfaite quand les mots-clés hésitent (borne haute)", True)):
            res = mesurer(oracle, jeu)
            bons = sum(1 for _, a, g, _ in res if a == g)
            print(f"\n{titre}\n  bien routées : {bons}/{len(res)} = {100 * bons / len(res):.0f} %")
            par_methode = {}
            for _, a, g, m in res:
                par_methode.setdefault(m, [0, 0])
                par_methode[m][1] += 1
                par_methode[m][0] += (a == g)
            for m, (b, t) in sorted(par_methode.items()):
                print(f"    décidé par « {m} » : {b}/{t} justes")
            if detail:
                for texte, a, g, m in res:
                    if a != g:
                        print(f"    ✘ [{m}] attendu {a:<20} obtenu {g:<20} « {texte[:70]} »")


if __name__ == "__main__":
    main()
