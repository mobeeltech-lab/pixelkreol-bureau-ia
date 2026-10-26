"""Grille de prix du 26/10/2026 + Kap Numérik suspendu : plus aucun ancien prix dans les fiches et le banc, et Kap Numérik n'est jamais présenté comme ouvert."""
import re
from pathlib import Path

import yaml

RACINE = Path(__file__).resolve().parent.parent
ESP = r"[\s  ]*"
ANCIENS_PRIX = {
    "Bureau Essentiel à 5 500 €": re.compile(r"\b5" + ESP + r"500" + ESP + r"(?:€|euros?\b)", re.I),
    "entretien à 25 € par mois": re.compile(r"\b25" + ESP + r"€(?:" + ESP + r"HT)?" + ESP + r"(?:/|par" + ESP + r")" + ESP + r"mois", re.I),
    "animation à 250 € par mois": re.compile(r"\b250" + ESP + r"€(?:" + ESP + r"HT)?" + ESP + r"(?:/|par" + ESP + r")" + ESP + r"mois", re.I),
    "majoration Corse": re.compile(r"majoration" + ESP + r"corse|corse" + ESP + r"[^.\n]{0,40}majoration|15" + ESP + r"(?:à|-)" + ESP + r"20" + ESP + r"%", re.I),
}
FICHIERS = [RACINE / "agents" / "00_commun.md", RACINE / "agents" / "registre.yaml", RACINE / "agents" / "socle.txt",
            *sorted((RACINE / "scripts" / "banc").glob("*.py")), RACINE / "scripts" / "banc" / "LISEZMOI.md"]
AGENTS = yaml.safe_load((RACINE / "agents" / "registre.yaml").read_text(encoding="utf-8"))["agents"]


def test_plus_aucun_ancien_prix_dans_les_fiches_et_le_banc():
    for f in FICHIERS:
        texte = f.read_text(encoding="utf-8")
        for nom, motif in ANCIENS_PRIX.items():
            assert not motif.search(texte), f"{f.relative_to(RACINE)} contient encore : {nom}"


def test_kap_numerik_nest_jamais_presente_comme_ouvert():
    commun = (RACINE / "agents" / "00_commun.md").read_text(encoding="utf-8")
    assert "SUSPENDU depuis décembre 2025" in commun and "Tu ne le\n  présentes jamais comme disponible" in commun
    for a in AGENTS:
        texte = re.sub(r"\s+", " ", a["consignes"])
        if re.search(r"kap[\s-]*num", texte, re.I):
            assert "suspendu" in texte.lower(), f"{a['id']} parle de Kap Numérik sans dire qu'il est suspendu"
            assert not re.search(r"argument kap|jusqu'à 80|financ\w+ à 80|financé à 70", texte, re.I), a["id"]
    subv = re.sub(r"\s+", " ", next(a["consignes"] for a in AGENTS if a["id"] == "subventions"))
    for morceau in ("17/12/2025", "31/12/2025", "version 2024, susceptible de changer", "ADEC (dispositif CRESCE)", "viser 2027"):
        assert morceau in subv, morceau
    assert "jamais de pourcentage de financement promis" in subv.lower()


def test_la_grille_du_banc_est_la_nouvelle():
    from sys import path
    path.insert(0, str(RACINE / "scripts"))
    from banc.regles import GRILLE
    assert {6900.0, 1990.0, 13900.0, 18000.0} <= GRILLE["bureau_ia"] and 5500.0 not in GRILLE["bureau_ia"]
    assert {1500.0, 2500.0, 4000.0, 250.0, 39.0, 390.0} == GRILLE["presence"]
    assert GRILLE["devis"] == {600.0, 480.0}


def test_la_fiche_devis_porte_la_nouvelle_grille():
    devis = re.sub(r"\s+", " ", next(a["consignes"] for a in AGENTS if a["id"] == "devis"))
    for texte in ("page supplémentaire 250 € HT", "entretien du site 39 € HT par mois", "animation des réseaux 390 € HT par mois",
                  "2 réseaux, 8 publications par mois, modération", "3 premiers mois d'animation des réseaux compris",
                  "journée 600 € HT pour le travail sur l'IA et les agents, 480 € HT pour le web", "× 480 € HT (web)",
                  "Déplacements : même grille HT partout", "sans majoration par région", "30 % à la signature"):
        assert texte in devis, texte
