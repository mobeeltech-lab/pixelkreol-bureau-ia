"""Fabrique un FAUX NAS (documents fictifs dans un dossier temporaire) pour les tests de l'outil « documents ».

Dépendances de TEST seulement (jamais installées sur le serveur) : reportlab, python-docx, openpyxl.
Tout est FICTIF. Les mots « SECRET-… » servent de sentinelles : ils ne doivent JAMAIS sortir de là où ils sont interdits.
"""
from __future__ import annotations

import os
from pathlib import Path

SECRET_PAIE = "SECRET-PAIE-FICTIF"
SECRET_IDENTITE = "SECRET-IDENTITE-FICTIF"
SECRET_EXTERNE = "SECRET-EXTERNE-FICTIF"
MOT_GROS = "MOT-RARE-DU-GROS-FICHIER"


def pdf(chemin: Path, lignes: list[str]) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(chemin), pagesize=A4)
    y = 800
    for ligne in lignes:
        c.drawString(60, y, ligne)
        y -= 18
    c.showPage()
    c.save()


def pdf_sans_texte(chemin: Path) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(chemin), pagesize=A4)
    c.rect(50, 50, 200, 200)            # un dessin, aucun texte : comme un scan sans OCR
    c.showPage()
    c.save()


def docx(chemin: Path, paragraphes: list[str], tableau: list[list[str]] | None = None) -> None:
    from docx import Document
    d = Document()
    for p in paragraphes:
        d.add_paragraph(p)
    if tableau:
        t = d.add_table(rows=len(tableau), cols=len(tableau[0]))
        for i, ligne in enumerate(tableau):
            for j, v in enumerate(ligne):
                t.cell(i, j).text = v
    d.save(str(chemin))


def xlsx(chemin: Path, feuilles: dict[str, list[list]]) -> None:
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for nom, lignes in feuilles.items():
        ws = wb.create_sheet(nom)
        for ligne in lignes:
            ws.append(ligne)
    wb.save(str(chemin))


def ecrire(chemin: Path, texte: str) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(texte, encoding="utf-8")


def fabriquer(racine: Path, dehors: Path, taille_gros: int = 1_300_000) -> dict:
    """Crée le faux NAS dans `racine` et un dossier `dehors` (HORS du NAS) pour les liens pièges."""
    racine.mkdir(parents=True, exist_ok=True)
    dehors.mkdir(parents=True, exist_ok=True)
    (racine / "Oliviers/acquereurs/FICTIF-DURAND").mkdir(parents=True)
    (racine / "Oliviers/chantier").mkdir(parents=True)
    (racine / "Cypres/livraison").mkdir(parents=True)
    (racine / "Cypres/baux").mkdir(parents=True)
    pdf(racine / "Oliviers/acquereurs/FICTIF-DURAND/acte-vefa-A02.pdf",
        ["DONNÉES FICTIVES — TEST", "ACTE DE VENTE EN L'ÉTAT FUTUR D'ACHÈVEMENT", "Lot A02 - acquéreur Alphonse FICTIF-DURAND",
         "Prix : 232 000,00 euros TTC", "Echéancier : 5 % à la signature, 30 % à l'achèvement des fondations"])
    xlsx(racine / "Oliviers/chantier/situation-travaux-4.xlsx",
         {"Situation 4": [["Entreprise", "Marché HT", "Avancement"], ["Maçonnerie EXEMPLE", 1200000, "62 %"],
                          ["Cumul demandé", 744000, "retenue de garantie 5 %"]], "Notes": [["Avancement constaté par le MOE : 58 %"]]})
    docx(racine / "Oliviers/chantier/planning-v3.docx",
         ["DONNÉES FICTIVES — TEST", "Planning du chantier Les Oliviers", "La mise hors d'eau est atteinte le 18/09/2026."],
         [["Lot de travaux", "Fin réelle"], ["Gros œuvre", "2026-09-18"]])
    ecrire(racine / "Cypres/livraison/PV-livraison-CYP-B12.txt",
           "DONNÉES FICTIVES — TEST\nProcès-verbal de livraison du lot B12 (programme Les Cyprès). Réserves : aucune. Clés remises le 2025-06-30.\n")
    ecrire(racine / "Cypres/baux/bail-A.csv", "DONNÉES FICTIVES — TEST\nlocataire;loyer;charges\nTEST-LOCATAIRE;650,00;45,00\n")
    ecrire(racine / "Rapport d'achèvement 2026.txt", "DONNÉES FICTIVES — TEST\nRapport sur l'achèvement des travaux.\n")
    # dossiers EXCLUS (invisibles)
    ecrire(racine / "RH/paie/bulletin-2026-09.txt", f"DONNÉES FICTIVES — TEST\nBulletin de paie {SECRET_PAIE} salaire 3000.\n")
    ecrire(racine / "Comptabilité/Pièces d'identité/carte-identite.txt", f"DONNÉES FICTIVES — TEST\n{SECRET_IDENTITE} carte d'identité.\n")
    # fichier trop gros (plus que la limite réglée dans les tests)
    (racine / "Archives").mkdir()
    (racine / "Archives/enorme.txt").write_text(("DONNÉES FICTIVES — TEST " + MOT_GROS + " ") + "x" * taille_gros, encoding="utf-8")
    # fichiers particuliers
    pdf_sans_texte(racine / "Archives/scan-sans-texte.pdf")
    (racine / "Archives/corrompu.pdf").write_bytes(b"%PDF-1.4 ceci n'est pas un vrai pdf \x00\xff\xfe")
    (racine / "Archives/photo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    # pièges : liens symboliques vers l'extérieur du NAS
    ecrire(dehors / "secret.txt", f"DONNÉES FICTIVES — TEST\n{SECRET_EXTERNE}\n")
    (racine / "piege").mkdir()
    os.symlink(dehors / "secret.txt", racine / "piege/lien-externe.txt")
    os.symlink(dehors, racine / "piege/lien-dossier")
    os.symlink(racine / "Cypres/livraison/PV-livraison-CYP-B12.txt", racine / "piege/lien-interne.txt")
    return {"racine": racine, "dehors": dehors}


def instantane(racine: Path) -> dict:
    """État complet d'un dossier (noms, tailles, dates de modification, liens) pour prouver qu'on n'a RIEN modifié."""
    etat = {}
    for dossier, sous, fichiers in os.walk(racine):
        for nom in sorted(sous + fichiers):
            p = Path(dossier) / nom
            st = p.lstat()
            etat[str(p.relative_to(racine))] = (st.st_size, st.st_mtime_ns, os.readlink(p) if p.is_symlink() else "")
    return etat


def copie_fraiche(modele: Path, dest: Path) -> Path:
    """Copie le faux NAS « modèle » dans `dest`/nas et recrée les liens pièges vers `dest`/dehors (jamais partagés entre tests)."""
    import shutil
    nas = dest / "nas"
    shutil.copytree(modele / "nas", nas, symlinks=True)
    shutil.copytree(modele / "dehors", dest / "dehors")
    for lien in nas.rglob("*"):
        if lien.is_symlink() and "dehors" in os.readlink(lien):
            cible = Path(os.readlink(lien))
            lien.unlink()
            os.symlink(dest / "dehors" / cible.name, lien)
    return nas
