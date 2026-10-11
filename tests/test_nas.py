"""Outil « documents » (lecture seule du NAS) — tests du module sur un FAUX NAS de documents fictifs."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestrateur"))
pytest.importorskip("reportlab")
pytest.importorskip("docx")
pytest.importorskip("openpyxl")
pytest.importorskip("pypdf")

import documents as d  # noqa: E402
import fabrique_nas as fab  # noqa: E402

TAILLE_MAX = 1024 * 1024


@pytest.fixture(scope="module")
def modele(tmp_path_factory):
    base = tmp_path_factory.mktemp("modele-nas")
    fab.fabriquer(base / "nas", base / "dehors")
    return base


@pytest.fixture
def nas(tmp_path, modele):
    """Copie fraîche du faux NAS (les liens symboliques sont recréés, pas copiés)."""
    return fab.copie_fraiche(modele, tmp_path)


def cfg_de(nas, tmp_path, **kw):
    base = dict(racine=str(nas), index=str(tmp_path / "index.sqlite"), taille_max=TAILLE_MAX, pause=0,
                caracteres_max=3000, delai=10, fuseau="Europe/Paris")
    base.update(kw)
    return d.Config(**base)


@pytest.fixture
def docs(nas, tmp_path):
    D = d.Documents(cfg_de(nas, tmp_path))
    D.indexer()
    return D


def chemins_indexes(D):
    with D._base() as con:
        return {r["chemin"] for r in con.execute("SELECT chemin FROM fichiers")}


def appel(D, outil, **args):
    return json.loads(D.executer(outil, args, "Testeur (id-1)", "archiviste"))


# =============================================================== lecture des formats
@pytest.mark.parametrize("chemin,mot", [
    ("Oliviers/acquereurs/FICTIF-DURAND/acte-vefa-A02.pdf", "232 000,00"),
    ("Oliviers/chantier/planning-v3.docx", "mise hors d'eau est atteinte"),
    ("Oliviers/chantier/situation-travaux-4.xlsx", "Maçonnerie EXEMPLE"),
    ("Cypres/livraison/PV-livraison-CYP-B12.txt", "lot B12"),
    ("Cypres/baux/bail-A.csv", "650,00"),
])
def test_lecture_de_chaque_format(docs, chemin, mot):
    r = appel(docs, "lire_document", chemin=chemin)
    assert r["lisible"] and r["chemin"] == chemin and mot in r["extrait"]


def test_docx_lit_aussi_les_tableaux_et_xlsx_toutes_les_feuilles(docs):
    assert "Gros œuvre" in appel(docs, "lire_document", chemin="Oliviers/chantier/planning-v3.docx")["extrait"]
    x = appel(docs, "lire_document", chemin="Oliviers/chantier/situation-travaux-4.xlsx")["extrait"]
    assert "Situation 4" in x and "Notes" in x and "58 %" in x and "744000" in x


@pytest.mark.parametrize("chemin,motif", [
    ("Archives/scan-sans-texte.pdf", "pas d'OCR"), ("Archives/corrompu.pdf", "illisible"),
    ("Archives/photo.png", "format non pris en charge"), ("Archives/enorme.txt", "trop gros")])
def test_fichiers_non_lisibles_sont_dits_sans_rien_inventer(docs, chemin, motif):
    r = appel(docs, "lire_document", chemin=chemin)
    assert r["lisible"] is False and motif in r["motif"] and r["chemin"] == chemin
    assert "extrait" not in r and fab.MOT_GROS not in json.dumps(r)


def test_encodages_latin1_et_windows(nas, tmp_path):
    (nas / "vieux.txt").write_bytes("Écriture comptable à valider — énoncé".encode("cp1252"))
    D = d.Documents(cfg_de(nas, tmp_path))
    assert "Écriture comptable" in appel(D, "lire_document", chemin="vieux.txt")["extrait"]


# =============================================================== indexation
def test_index_contient_les_documents_visibles_et_rien_d_autre(docs):
    attendu = {"Oliviers/acquereurs/FICTIF-DURAND/acte-vefa-A02.pdf", "Oliviers/chantier/situation-travaux-4.xlsx",
               "Oliviers/chantier/planning-v3.docx", "Cypres/livraison/PV-livraison-CYP-B12.txt", "Cypres/baux/bail-A.csv",
               "Rapport d'achèvement 2026.txt", "Archives/enorme.txt", "Archives/scan-sans-texte.pdf",
               "Archives/corrompu.pdf", "Archives/photo.png"}
    assert chemins_indexes(docs) == attendu


def test_reprise_sans_tout_relire(nas, tmp_path):
    D = d.Documents(cfg_de(nas, tmp_path))
    premier = D.indexer()
    assert premier["indexes"] == 10 and premier["complet"]
    second = D.indexer()
    assert second["indexes"] == 0 and second["inchanges"] == 10
    (nas / "Cypres/baux/bail-A.csv").write_text("DONNÉES FICTIVES — TEST\nlocataire;loyer\nTEST-LOCATAIRE;659,75\n", encoding="utf-8")
    ajout = nas / "Nouveau-document.txt"
    ajout.write_text("DONNÉES FICTIVES — TEST nouveau", encoding="utf-8")
    troisieme = D.indexer()
    assert troisieme["indexes"] == 2 and troisieme["inchanges"] == 9
    assert "659,75" in appel(D, "lire_document", chemin="Cypres/baux/bail-A.csv")["extrait"]
    assert appel(D, "rechercher_documents", texte="659,75")["trouve"]


def test_fichier_supprime_du_nas_disparait_de_l_index(nas, tmp_path):
    D = d.Documents(cfg_de(nas, tmp_path))
    D.indexer()
    (nas / "Cypres/baux/bail-A.csv").unlink()
    bilan = D.indexer()
    assert bilan["purges"] == 1 and "Cypres/baux/bail-A.csv" not in chemins_indexes(D)


def test_indexation_interrompue_reprend_ou_elle_s_est_arretee_sans_rien_purger(nas, tmp_path):
    D = d.Documents(cfg_de(nas, tmp_path))
    coupee = D.indexer(delai=0)                      # délai écoulé tout de suite
    assert coupee["complet"] is False and coupee["purges"] == 0
    assert coupee["indexes"] < 10
    suite = D.indexer()
    assert suite["complet"] and len(chemins_indexes(D)) == 10


def test_nas_non_monte_ou_vide_ne_vide_pas_l_index(nas, tmp_path):
    D = d.Documents(cfg_de(nas, tmp_path))
    D.indexer()
    avant = chemins_indexes(D)
    import shutil
    for enfant in nas.iterdir():
        shutil.rmtree(enfant) if enfant.is_dir() and not enfant.is_symlink() else enfant.unlink()
    bilan = D.indexer()
    assert "NAS indisponible" in bilan["message"] and chemins_indexes(D) == avant
    assert appel(D, "rechercher_documents", texte="achèvement")["trouve"]          # la recherche sur l'index continue


def test_suppression_massive_refusee(nas, tmp_path):
    D = d.Documents(cfg_de(nas, tmp_path))
    for i in range(30):
        (nas / f"lot-{i:02d}.txt").write_text(f"DONNÉES FICTIVES — TEST dossier {i}", encoding="utf-8")
    D.indexer()
    n = len(chemins_indexes(D))
    for i in range(30):
        (nas / f"lot-{i:02d}.txt").unlink()
    bilan = D.indexer()
    assert "suppression massive refusée" in bilan["message"] and len(chemins_indexes(D)) == n


def test_une_seule_indexation_a_la_fois(nas, tmp_path):
    D = d.Documents(cfg_de(nas, tmp_path))
    assert D.verrou.acquire(blocking=False)
    try:
        assert D.indexer() == {"deja_en_cours": True}
    finally:
        D.verrou.release()


def test_dossier_devenu_exclu_sort_de_l_index(nas, tmp_path):
    D = d.Documents(cfg_de(nas, tmp_path))
    D.indexer()
    assert any(c.startswith("Cypres/baux") for c in chemins_indexes(D))
    D2 = d.Documents(cfg_de(nas, tmp_path, exclus=("baux",)))
    D2.indexer()
    assert not any(c.startswith("Cypres/baux") for c in chemins_indexes(D2))
    assert "Cypres/baux/bail-A.csv" not in [x["chemin"] for x in appel(D2, "rechercher_documents", nom="bail")["resultats"]]
    assert "Cypres/baux/bail-A.csv" not in [x["chemin"] for x in appel(D2, "rechercher_documents", texte="TEST-LOCATAIRE loyer charges")["resultats"]]


# =============================================================== recherche
def test_recherche_dans_le_texte_sans_tenir_compte_des_accents_ni_des_majuscules(docs):
    for requete in ("achèvement fondations", "ACHEVEMENT FONDATIONS"):
        r = appel(docs, "rechercher_documents", texte=requete)
        assert r["trouve"] and r["resultats"][0]["chemin"] == "Oliviers/acquereurs/FICTIF-DURAND/acte-vefa-A02.pdf"
    assert "Oliviers/acquereurs/FICTIF-DURAND/acte-vefa-A02.pdf" in [x["chemin"] for x in appel(docs, "rechercher_documents", texte="achevement")["resultats"]]
    r = appel(docs, "rechercher_documents", texte="232 000")
    assert "232 000,00" in r["resultats"][0]["extrait"]


def test_recherche_par_nom_dossier_type_et_date(docs):
    assert [x["chemin"] for x in appel(docs, "rechercher_documents", nom="PV livraison")["resultats"]] == ["Cypres/livraison/PV-livraison-CYP-B12.txt"]
    r = appel(docs, "rechercher_documents", dossier="oliviers chantier")
    assert {x["chemin"] for x in r["resultats"]} == {"Oliviers/chantier/planning-v3.docx", "Oliviers/chantier/situation-travaux-4.xlsx"}
    assert {x["type"] for x in appel(docs, "rechercher_documents", type="pdf")["resultats"]} == {"pdf"}
    assert len(appel(docs, "rechercher_documents", type="docx,xlsx")["resultats"]) == 2
    aujourdhui = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d")
    assert appel(docs, "rechercher_documents", type="pdf", apres=aujourdhui)["trouve"]
    assert appel(docs, "rechercher_documents", type="pdf", avant="2001-01-01")["trouve"] is False
    assert "Date invalide" in appel(docs, "rechercher_documents", apres="hier")["message"]


def test_resultat_avec_chemin_exact_et_extrait(docs):
    r = appel(docs, "rechercher_documents", texte="livraison lot B12")
    premier = r["resultats"][0]
    assert premier["chemin"] == "Cypres/livraison/PV-livraison-CYP-B12.txt" and "B12" in premier["extrait"]
    assert (docs.racine and os.path.isfile(os.path.join(docs.racine, premier["chemin"])))      # le chemin cité existe vraiment
    assert set(premier) >= {"chemin", "nom", "type", "taille_octets", "modifie", "extrait"}


def test_rien_trouve_est_dit_sans_rien_inventer(docs):
    r = appel(docs, "rechercher_documents", texte="licorne-introuvable-xyz")
    assert r["trouve"] is False and r["resultats"] == [] and "Aucun document" in r["message"]
    assert appel(docs, "rechercher_documents")["trouve"] is False                       # aucun critère
    assert appel(docs, "rechercher_documents", nom="   ")["trouve"] is False


def test_correspondance_partielle_signalee(docs):
    r = appel(docs, "rechercher_documents", texte="livraison licorne")
    assert r["trouve"] and "partielle" in r["message"]


@pytest.mark.parametrize("requete", ["\"; DROP TABLE fichiers; --", "a* OR b", "NEAR(a b)", "\"", "'", "%", "_", "\\", "(((", "col:valeur",
                                     "x" * 5000])
def test_requetes_bizarres_ne_cassent_rien(docs, requete):
    r = appel(docs, "rechercher_documents", texte=requete)
    assert "resultats" in r
    with docs._base() as con:
        assert con.execute("SELECT COUNT(*) FROM fichiers").fetchone()[0] == 10


def test_limite_de_resultats_et_de_caracteres(nas, tmp_path):
    for i in range(60):
        (nas / f"note-{i:02d}.txt").write_text("DONNÉES FICTIVES — TEST " + ("situation de travaux avancement " * 40), encoding="utf-8")
    D = d.Documents(cfg_de(nas, tmp_path, caracteres_max=1200))
    D.indexer()
    r = json.loads(D.executer("rechercher_documents", {"texte": "avancement", "limite": 50}))
    assert len(json.dumps(r, ensure_ascii=False)) <= 1200 + 1500 and r["tronque"] is True      # budget de caractères
    large = d.Documents(cfg_de(nas, tmp_path, caracteres_max=50_000, index=str(tmp_path / "index2.sqlite")))
    large.indexer()
    assert len(json.loads(large.executer("rechercher_documents", {"texte": "avancement", "limite": 50}))["resultats"]) == 20   # plafond dur
    assert len(json.loads(large.executer("rechercher_documents", {"texte": "avancement", "limite": 3}))["resultats"]) == 3


def test_sans_index_recherche_par_nom_bornee_par_fichiers_max(nas, tmp_path):
    for i in range(80):
        (nas / f"aaa-{i:02d}.txt").write_text("x", encoding="utf-8")
    D = d.Documents(cfg_de(nas, tmp_path, fichiers_max=10))        # pas d'indexation : index vide
    r = appel(D, "rechercher_documents", nom="aaa")
    assert r["trouve"] and len(r["resultats"]) <= 8 and "index pas encore construit" in r["resultats"][0]["note"]
    D.cfg.fichiers_max = 5
    pas_vu = appel(D, "rechercher_documents", nom="PV livraison")         # le fichier existe mais dépasse les 5 premiers
    assert pas_vu["trouve"] is False or all(x["chemin"].startswith(("Archives", "Cypres", "Oliviers", "aaa")) for x in pas_vu["resultats"])


def test_delai_maximal_respecte_pendant_la_recherche(nas, tmp_path, monkeypatch):
    for i in range(30):
        (nas / f"n-{i:02d}.txt").write_text("DONNÉES FICTIVES — TEST avancement", encoding="utf-8")
    D = d.Documents(cfg_de(nas, tmp_path, delai=2))
    D.indexer()
    reel = time.monotonic
    depart = reel()
    monkeypatch.setattr(d.time, "monotonic", lambda: reel() + (1000 if reel() - depart >= 0 else 0))   # le temps « saute » au-delà du délai
    r = json.loads(D.executer("rechercher_documents", {"texte": "avancement"}))
    assert r.get("tronque") is True or r["resultats"] == [] or len(r["resultats"]) < 30


# =============================================================== invisibilité des dossiers exclus
@pytest.mark.parametrize("requete", [dict(texte=fab.SECRET_PAIE), dict(nom="bulletin"), dict(dossier="paie"), dict(dossier="RH"),
                                     dict(texte=fab.SECRET_IDENTITE), dict(nom="carte identite"), dict(dossier="Pièces d'identité"),
                                     dict(texte="bulletin de paie salaire"), dict(type="txt", nom="identite")])
def test_dossiers_exclus_invisibles_a_la_recherche(docs, requete):
    r = appel(docs, "rechercher_documents", **requete)
    assert not any(re.search(r"RH|paie|identit", x["chemin"], re.I) for x in r.get("resultats", []))
    assert fab.SECRET_PAIE not in json.dumps(r) and fab.SECRET_IDENTITE not in json.dumps(r)


@pytest.mark.parametrize("chemin", ["RH/paie/bulletin-2026-09.txt", "rh/PAIE/bulletin-2026-09.txt", "Comptabilité/Pièces d'identité/carte-identite.txt",
                                    "Comptabilite/Pieces d'identite/carte-identite.txt"])
def test_dossiers_exclus_invisibles_a_la_lecture_meme_message_qu_un_fichier_absent(docs, chemin):
    r = appel(docs, "lire_document", chemin=chemin)
    absent = appel(docs, "lire_document", chemin="Cypres/inexistant.txt")
    assert r == {**absent}                                    # impossible de savoir qu'un dossier exclu existe
    assert fab.SECRET_PAIE not in json.dumps(r)


def test_exclusions_reglables_et_par_mot_entier(nas, tmp_path):
    D = d.Documents(cfg_de(nas, tmp_path, exclus=("baux", "*.csv", "Rapport d'achèvement")))
    assert D.exclu("Cypres/baux/x.txt") and D.exclu("a/b.csv") and D.exclu("Rapport d'achevement 2026.txt")
    assert not D.exclu("Oliviers/chantier/planning-v3.docx")
    D2 = d.Documents(cfg_de(nas, tmp_path))                  # liste par défaut : « rh » = mot entier, pas une syllabe
    assert D2.exclu("Dossiers RH/x.pdf") and D2.exclu("RH-2026/x.pdf") and not D2.exclu("Rhône/x.pdf") and not D2.exclu("orchestre/x.pdf")
    assert d.Config.depuis_env({"PK_NAS_EXCLUS": ""}).exclus == d.EXCLUS_PAR_DEFAUT          # vide = défaut, jamais « rien exclu »
    assert d.Config.depuis_env({"PK_NAS_EXCLUS": "-"}).exclus == ()
    assert d.Config.depuis_env({"PK_NAS_EXCLUS": "a, b"}).exclus == ("a", "b")


# =============================================================== confinement
@pytest.mark.parametrize("chemin", ["../dehors/secret.txt", "../../etc/passwd", "Cypres/../../dehors/secret.txt", "/etc/passwd",
                                    "/tmp", "~/secret", "..", ".", "", "   ", "C:/Windows/win.ini", "Cypres\\..\\..\\x", "a\x00b",
                                    "Cypres/livraison/../../../etc/passwd", "%2e%2e/%2e%2e/etc/passwd", "....//....//etc/passwd",
                                    "Cypres/../Oliviers/chantier/planning-v3.docx"])      # même un « .. » qui reste dans le NAS est refusé
def test_sortie_du_dossier_du_nas_refusee(docs, chemin):
    r = appel(docs, "lire_document", chemin=chemin)
    assert r.get("lisible") is not True and "erreur" in r
    assert fab.SECRET_EXTERNE not in json.dumps(r) and "root:" not in json.dumps(r)


def test_liens_symboliques_invisibles_partout(docs, nas):
    assert not any(c.startswith("piege") for c in chemins_indexes(docs))
    for chemin in ("piege/lien-externe.txt", "piege/lien-dossier/secret.txt", "piege/lien-interne.txt"):
        r = appel(docs, "lire_document", chemin=chemin)
        assert "erreur" in r and fab.SECRET_EXTERNE not in json.dumps(r)
    assert appel(docs, "rechercher_documents", texte=fab.SECRET_EXTERNE)["trouve"] is False
    assert appel(docs, "rechercher_documents", dossier="piege")["trouve"] is False


def test_lien_symbolique_cree_apres_l_indexation_est_refuse_aussi(docs, nas, tmp_path):
    os.symlink(tmp_path / "dehors" / "secret.txt", nas / "Cypres/lien-tardif.txt")
    assert "erreur" in appel(docs, "lire_document", chemin="Cypres/lien-tardif.txt")
    docs.indexer()
    assert "Cypres/lien-tardif.txt" not in chemins_indexes(docs)


def test_racine_elle_meme_derriere_un_lien_est_resolue_une_fois(nas, tmp_path):
    os.symlink(nas, tmp_path / "montage")
    D = d.Documents(cfg_de(tmp_path / "montage", tmp_path))
    D.indexer()
    assert appel(D, "lire_document", chemin="Cypres/baux/bail-A.csv")["lisible"]
    assert len(chemins_indexes(D)) == 10


def test_dossier_et_chemin_inexistant_refuses(docs):
    for chemin in ("Oliviers", "Oliviers/chantier", "Oliviers/inexistant.pdf"):
        assert "erreur" in appel(docs, "lire_document", chemin=chemin)


def test_chemin_unicode_nfd_macos_accepte(docs):
    nfd = unicodedata.normalize("NFD", "Rapport d'achèvement 2026.txt")
    if nfd == "Rapport d'achèvement 2026.txt":
        pytest.skip("pas de différence NFC/NFD")
    r = appel(docs, "lire_document", chemin=nfd)
    assert r.get("lisible") is True or "erreur" in r        # accepté ou refusé proprement, jamais d'erreur 500


# =============================================================== lecture seule
def test_aucune_ecriture_dans_le_nas_apres_tout_usage(docs, nas):
    avant = fab.instantane(nas)
    docs.indexer()
    for outil, args in [("rechercher_documents", dict(texte="achèvement")), ("lire_document", dict(chemin="Cypres/baux/bail-A.csv")),
                        ("lire_document", dict(chemin="../../etc/passwd")), ("lire_document", dict(chemin="RH/paie/bulletin-2026-09.txt")),
                        ("supprimer_document", dict(chemin="Cypres/baux/bail-A.csv")), ("deplacer_document", dict(chemin="a", vers="b")),
                        ("renommer_document", dict(chemin="a", nom="b")), ("ecrire_document", dict(chemin="x.txt", contenu="x"))]:
        docs.executer(outil, args, "t", "archiviste")
    assert fab.instantane(nas) == avant


@pytest.mark.parametrize("outil", ["ecrire_document", "supprimer_document", "deplacer_document", "renommer_document", "modifier_document",
                                   "creer_document", "copier_document", "write_file", "delete_file", "rm", "N'importe quoi", ""])
def test_outils_d_ecriture_refuses_et_journalises(docs, outil):
    r = appel(docs, outil, chemin="Cypres/baux/bail-A.csv", contenu="piraté")
    assert "LECTURE SEULE" in r["erreur"]
    with docs._base() as con:
        assert con.execute("SELECT COUNT(*) FROM journal WHERE action='refus' AND ok=0").fetchone()[0] >= 1


def test_le_code_n_essaie_meme_pas_d_ecrire_dans_le_nas():
    source = (Path(__file__).resolve().parent.parent / "orchestrateur" / "documents.py").read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""', "", source)
    code = "\n".join(l for l in code.splitlines() if not l.strip().startswith("#"))
    interdits = [r"\bos\.(remove|unlink|rename|renames|replace|rmdir|removedirs|mkdir|makedirs|chmod|chown|utime|truncate|symlink|link|mkfifo|mknod)\b",
                 r"\bshutil\b", r"\.(unlink|rmdir|rename|write_text|write_bytes|touch|mkdir|chmod|symlink_to|hardlink_to)\(",
                 r"\bopen\([^)]*['\"][wax+]", r"O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND", r"\bsubprocess\b", r"\bos\.system\b", r"\bos\.popen\b"]
    trouves = [m.group(0) for p in interdits for m in re.finditer(p, code)]
    assert not trouves, trouves
    assert "O_RDONLY" in code and "O_NOFOLLOW" in code


def test_les_fichiers_du_nas_sont_ouverts_en_lecture_seule_sans_suivre_les_liens(nas):
    with d.ouvrir_lecture(str(nas / "Cypres/baux/bail-A.csv")) as f:
        assert f.readable() and not f.writable()
        with pytest.raises(Exception):
            f.write(b"x")
    with pytest.raises(OSError):
        d.ouvrir_lecture(str(nas / "piege/lien-interne.txt"))               # O_NOFOLLOW


def test_nas_monte_en_lecture_seule_n_est_pas_un_probleme(nas, tmp_path):
    """Si le système de fichiers est en lecture seule, l'outil fonctionne (il n'écrit que son index, ailleurs)."""
    D = d.Documents(cfg_de(nas, tmp_path))
    D.indexer()
    assert not str(D.cfg.index).startswith(str(nas))
    for p in nas.rglob("*"):
        if p.is_file() and not p.is_symlink():
            p.chmod(0o444)
    try:
        assert appel(D, "lire_document", chemin="Cypres/baux/bail-A.csv")["lisible"]
        assert D.indexer()["complet"]
    finally:
        for p in nas.rglob("*"):
            if p.is_file() and not p.is_symlink():
                p.chmod(0o644)


# =============================================================== limites
def test_taille_maximale_reglable(nas, tmp_path):
    D = d.Documents(cfg_de(nas, tmp_path, taille_max=300))
    D.indexer()
    r = appel(D, "lire_document", chemin="Oliviers/chantier/planning-v3.docx")
    assert r["lisible"] is False and "trop gros" in r["motif"]


def test_un_extrait_jamais_le_document_entier(nas, tmp_path):
    (nas / "long.txt").write_text("DONNÉES FICTIVES — TEST " + "alpha " * 5000 + " FIN-UNIQUE", encoding="utf-8")
    D = d.Documents(cfg_de(nas, tmp_path, caracteres_max=800))
    D.indexer()
    r = appel(D, "lire_document", chemin="long.txt")
    assert len(r["extrait"]) <= 800 and "FIN-UNIQUE" not in r["extrait"]
    assert r["suite_disponible"] and r["debut_suivant"] == 800 and r["caracteres_total"] > 20000
    suite = appel(D, "lire_document", chemin="long.txt", debut=r["debut_suivant"])
    assert suite["debut"] == 800 and len(suite["extrait"]) <= 800
    p = appel(D, "lire_document", chemin="long.txt", mots="fin-unique")
    assert any("FIN-UNIQUE" in x for x in p["passages"]) and sum(map(len, p["passages"])) <= 800 + 600
    assert len(D.executer("lire_document", {"chemin": "long.txt"}, "t", "a")) <= 800 + 1500


def test_texte_garde_dans_l_index_borne(nas, tmp_path):
    (nas / "long.txt").write_text("mot " * 100_000, encoding="utf-8")
    D = d.Documents(cfg_de(nas, tmp_path, texte_max=5000))
    D.indexer()
    with D._base() as con:
        assert con.execute("SELECT LENGTH(texte) FROM fichiers WHERE chemin='long.txt'").fetchone()[0] <= 5000


def test_zip_bomb_docx_refuse(nas, tmp_path):
    import zipfile
    chemin = nas / "bombe.docx"
    with zipfile.ZipFile(chemin, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:p><w:t>"
                   + "A" * (70 * 1024 * 1024) + "</w:t></w:p></w:document>")
    assert chemin.stat().st_size < TAILLE_MAX
    D = d.Documents(cfg_de(nas, tmp_path))
    D.indexer()
    assert appel(D, "lire_document", chemin="bombe.docx")["lisible"] is False


def test_xml_malveillant_ne_plante_pas(nas, tmp_path):
    import zipfile
    with zipfile.ZipFile(nas / "piege.docx", "w") as z:
        z.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:t>&b;</w:t></w:p></w:document>')
    D = d.Documents(cfg_de(nas, tmp_path))
    assert D.indexer()["complet"]
    assert "lisible" in appel(D, "lire_document", chemin="piege.docx")


# =============================================================== journal
def test_journal_qui_quoi_quand_jamais_le_contenu(docs):
    docs.executer("rechercher_documents", {"texte": "achèvement fondations"}, "Marie Test (id-42)", "archiviste")
    docs.executer("lire_document", {"chemin": "Cypres/livraison/PV-livraison-CYP-B12.txt"}, "Marie Test (id-42)", "archiviste")
    docs.executer("lire_document", {"chemin": "../../etc/passwd"}, "Paul Test (id-7)", "comptable")
    with docs._base() as con:
        lignes = [dict(r) for r in con.execute("SELECT * FROM journal ORDER BY id")]
    assert [l["action"] for l in lignes] == ["recherche", "lecture", "refus"]
    assert lignes[0]["demandeur"] == "Marie Test (id-42)" and lignes[0]["agent"] == "archiviste"
    assert "acte-vefa-A02.pdf" in lignes[0]["cible"] and lignes[1]["cible"] == "Cypres/livraison/PV-livraison-CYP-B12.txt"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", lignes[0]["ts"])
    assert lignes[2]["ok"] == 0 and lignes[2]["demandeur"] == "Paul Test (id-7)"
    tout = json.dumps(lignes, ensure_ascii=False)
    for contenu in ("232 000,00", "Procès-verbal de livraison", "Réserves : aucune", "Clés remises", "DONNÉES FICTIVES"):
        assert contenu not in tout, f"contenu de document dans le journal : {contenu}"


def test_journal_ne_garde_jamais_le_texte_des_documents_meme_dans_la_cible(docs):
    docs.executer("lire_document", {"chemin": "Cypres/baux/bail-A.csv", "mots": "650,00 loyer"}, "x", "locatif")
    with docs._base() as con:
        cibles = [r["cible"] for r in con.execute("SELECT cible FROM journal")]
    assert cibles == ["Cypres/baux/bail-A.csv"]


# =============================================================== réglages et désactivation
def test_reglages_par_defaut_et_bornes():
    c = d.Config.depuis_env({})
    assert c.racine == "" and c.mode == "auto" and c.nuit == "02:30" and c.tours_max == 4
    c = d.Config.depuis_env({"PK_NAS_TAILLE_MAX_MO": "9999", "PK_NAS_DELAI": "0", "PK_NAS_FICHIERS_MAX": "abc", "PK_NAS_MODE": "n'importe"})
    assert c.taille_max == 500 * 1024 * 1024 and c.delai == 2 and c.fichiers_max == 3000 and c.mode == "auto"


def test_chemin_du_nas_obligatoirement_absolu(tmp_path):
    with pytest.raises(ValueError):
        d.Documents(d.Config(racine="relatif/nas", index=str(tmp_path / "i.sqlite")))
    with pytest.raises(ValueError):
        d.Documents(d.Config(racine="", index=str(tmp_path / "i.sqlite")))


def test_prochaine_execution_de_la_nuit():
    tz = ZoneInfo("Europe/Paris")
    assert d.prochaine_execution(datetime(2026, 10, 11, 1, 0, tzinfo=tz), "02:30") == datetime(2026, 10, 11, 2, 30, tzinfo=tz)
    assert d.prochaine_execution(datetime(2026, 10, 11, 3, 0, tzinfo=tz), "02:30") == datetime(2026, 10, 12, 2, 30, tzinfo=tz)
    assert d.prochaine_execution(datetime(2026, 10, 11, 2, 30, tzinfo=tz), "02:30") == datetime(2026, 10, 12, 2, 30, tzinfo=tz)
    assert d.prochaine_execution(datetime(2026, 10, 11, 1, 0, tzinfo=tz), "nimporte quoi") == datetime(2026, 10, 11, 2, 30, tzinfo=tz)


def test_chemins_cites_dans_une_reponse():
    t = "Voir `Oliviers/chantier/planning-v3.docx`, puis « acte.pdf » et (bail-A.csv). Le fichier note.PDF aussi, pas photo.png."
    assert d.chemins_cites(t) == ["Oliviers/chantier/planning-v3.docx", "acte.pdf", "bail-A.csv", "note.PDF"]


def test_chemins_cites_sans_les_marques_markdown():
    """Gras (**) et backticks autour d'un nom ne font pas partie du nom (faux « absent du NAS » constaté)."""
    t = ("**acte-vefa-A02.pdf**, `bail-A.csv`, **`Oliviers/chantier/planning-v3.docx`**, `**note.PDF**`, "
         "**Oliviers/juridique/contrat.pdf** et __gras__ `a.txt`. Liste : - **tableau.xlsx** :")
    assert d.chemins_cites(t) == ["acte-vefa-A02.pdf", "bail-A.csv", "Oliviers/chantier/planning-v3.docx", "note.PDF",
                                  "Oliviers/juridique/contrat.pdf", "a.txt", "tableau.xlsx"]
    assert d.chemins_cites("**gras sans fichier** et `code`") == []


def test_dependances_legeres():
    """Le module n'importe que la bibliothèque standard (pypdf seulement à la demande)."""
    source = (Path(__file__).resolve().parent.parent / "orchestrateur" / "documents.py").read_text(encoding="utf-8")
    haut = source.split("class AccesRefuse")[0]
    modules = set(re.findall(r"^(?:import|from) (\w+)", haut, re.M))
    assert modules <= {"__future__", "fnmatch", "json", "logging", "os", "re", "sqlite3", "stat", "threading", "time", "unicodedata",
                       "zipfile", "contextlib", "dataclasses", "datetime", "pathlib", "typing", "xml", "zoneinfo"}, modules


def test_un_document_exclu_present_par_erreur_dans_l_index_ne_sort_jamais(docs):
    """Défense en profondeur : même si une ligne exclue (ou issue d'une ancienne configuration) traînait dans l'index."""
    with docs._base() as con:
        cur = con.execute("""INSERT INTO fichiers(chemin, nom, nom_norm, dossier, dossier_norm, ext, type, taille, mtime_ns, modifie, indexe_le,
                             statut, texte) VALUES('RH/paie/fantome.txt','fantome.txt','fantome.txt','RH/paie','rh/paie','.txt','txt',10,1,
                             '2026-10-01','2026-10-01','ok',?)""", (f"{fab.SECRET_PAIE} fantome",))
        con.execute("INSERT INTO fts(rowid, nom, dossier, texte) VALUES(?,?,?,?)", (cur.lastrowid, "fantome.txt", "RH/paie", f"{fab.SECRET_PAIE} fantome"))
    for requete in (dict(texte="fantome"), dict(nom="fantome"), dict(dossier="paie"), dict(texte=fab.SECRET_PAIE)):
        r = appel(docs, "rechercher_documents", **requete)
        assert "fantome" not in json.dumps(r) and fab.SECRET_PAIE not in json.dumps(r), requete
    assert docs.nom_existe("fantome.txt") is False
    assert docs.indexer()["purges"] == 1                              # et la passe suivante le retire de l'index
