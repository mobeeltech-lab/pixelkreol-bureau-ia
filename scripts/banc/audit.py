"""Audit STATIQUE des fiches d'agents (sans modèle) : la fiche contient-elle de quoi produire une bonne réponse ?

Ce n'est PAS une note de qualité des réponses (seul le banc contre l'orchestrateur la mesure) : c'est une grille de complétude des fiches, utile pour voir
avant/après un changement de fiche sans avoir besoin des modèles. Nécessite PyYAML (présent dans le conteneur pk_orchestrateur et dans les tests).
"""
from __future__ import annotations

import re
from pathlib import Path

from .regles import sans_accents

CRITERES = [
    ("mission", "mission détaillée (≥ 350 caractères)", lambda c, a, ctx: len(c) >= 350),
    ("format", "format de sortie défini", lambda c, a, ctx: re.search(r"format|tableau|livrable|structure|colonnes|sections|plan en|modele|gabarit|objet \+|etapes", c) is not None),
    ("feux", "règle des feux propre à l'agent", lambda c, a, ctx: len(re.findall(r"orange|rouge|vert", c)) >= 2),
    ("renvois", "renvois nommés vers ≥ 2 autres agents", lambda c, a, ctx: len([n for n in ctx["noms"] if n != a and n in c]) >= 2),
    ("interdits", "interdits explicites (≥ 2)", lambda c, a, ctx: len(re.findall(r"\bjamais\b|\bne [^.;]{0,40} pas\b|\binterdit|\brefuse", c)) >= 2),
    ("cas_limites", "cas limites traités (≥ 2 « si / en cas »)", lambda c, a, ctx: len(re.findall(r"(?:^|[ .;:(])(si |en cas|quand |lorsque)", c)) >= 2),
    ("exemple", "exemple concret", lambda c, a, ctx: re.search(r"exemple|\bex\. |par ex|« [^»]{12,} »", c) is not None),
    ("cadrage", "questions de cadrage / information manquante", lambda c, a, ctx: re.search(r"(pose|demande|poser)[^.]{0,40}question|questions? (de cadrage|si)|demande (le|la|les|a|au)|si (une|l.)information|manque", c) is not None),
    ("honnetete", "honnêteté sur ce qu'il ne sait pas (n'invente pas, à confirmer…)", lambda c, a, ctx: re.search(r"n.invente|a confirmer|a verifier|ne sais pas|source|introuvable|sans (inventer|chiffre)", c) is not None),
    ("ton", "ton / destinataire précisé (vouvoiement, client, Will)", lambda c, a, ctx: re.search(r"vouvoi|tutoi|ton |au client|destinataire|a will", c) is not None),
]


def charger_registre(dossier: Path) -> list[dict]:
    import yaml
    agents = []
    for nom in ("registre.yaml", "perso.yaml"):
        f = Path(dossier) / nom
        if f.is_file():
            agents += (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("agents") or []
    return agents


def auditer(dossier: Path) -> dict[str, dict]:
    """{id_agent: {"score": 0..1, "manque": [libellés], "ok": [clés]}}"""
    agents = charger_registre(dossier)
    ctx = {"noms": []}
    for a in agents:
        ctx["noms"] += [sans_accents(a["id"]).replace("-", " "), sans_accents(a["nom"])]
    sortie = {}
    for a in agents:
        propres = {sans_accents(a["id"]).replace("-", " "), sans_accents(a["nom"])}
        autres = {"noms": [n for n in ctx["noms"] if n not in propres]}
        c = sans_accents(a.get("consignes", "")).replace("’", "'")
        ok, manque = [], []
        for cle, lib, test in CRITERES:
            if test(c, a["id"], autres):
                ok.append(cle)
            else:
                manque.append(lib)
        sortie[a["id"]] = {"score": len(ok) / len(CRITERES), "ok": ok, "manque": manque, "taille": len(a.get("consignes", ""))}
    return sortie
