"""Pages du site : grille du 26/10/2026, Kap Numérik retiré des arguments commerciaux, prix des packs cohérents avec la grille."""
import re
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
PAGES = ["index-improved.html", "pixelkreol-bureau-ia-improved.html", "secteurs-improved.html", "tarification-bureau-ia.html", "pixelkreol-agents-communication.html"]
ANCIENS = [r"\b5[\s  ]?500[\s  ]?€", r'"price":"5500"', r"\b3[\s  ]?900[\s  ]?€", r"\b13[\s  ]?500[\s  ]?€",
           r"1[\s  ]?500[\s  ]?€[\s  ]?TTC", r"vitrine dès 1[\s  ]?200"]


def texte(page):
    return (RACINE / page).read_text(encoding="utf-8")


def test_plus_aucun_ancien_prix_sur_le_site():
    for page in PAGES:
        for motif in ANCIENS:
            assert not re.search(motif, texte(page)), (page, motif)


def test_kap_numerik_nest_plus_un_argument_commercial():
    for page in PAGES:
        sans_css = re.sub(r"<style.*?</style>", "", texte(page), flags=re.S)
        assert not re.search(r"kap[\s-]*num|id=\"kap\"|href=\"#kap\"", sans_css, re.I), page


def test_les_packs_suivent_la_grille():
    t = texte("secteurs-improved.html")
    packs = re.findall(r'<div class="pack-h"><h3>(.*?)</h3><div class="niv">(.*?)</div>', t)
    assert len(packs) == 36
    autorises = {"dès 6 900 € HT", "dès 13 900 € HT", "dès 1 990 € TTC", "1 990 € TTC", "sur devis"}
    for nom, niv in packs:
        prix = re.sub(r"<[^>]+>", "", niv).split("—")[-1].strip()
        assert prix in autorises, (nom, prix)


def test_les_prix_de_la_grille_sont_sur_les_pages_tarifaires():
    bureau = texte("pixelkreol-bureau-ia-improved.html")
    assert '"price":"1990"' in bureau and '"price":"6900"' in bureau and '"price":"13900"' in bureau
    tarif = texte("tarification-bureau-ia.html")
    assert "6 900€" in tarif and "13 900€" in tarif and "1 990€" in tarif
    index = texte("index-improved.html")
    assert "1 500 € HT · page supplémentaire 250 € HT · entretien 39 € HT/mois" in index and "390 € HT / mois" in index and "6 900 € HT" in index
