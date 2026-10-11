"""Mesure : temps et mémoire de l'indexation de N fichiers FICTIFS (5 000 par défaut), reprise, recherche.

Usage : python tests/mesure_nas.py [--fichiers 5000]
Le NAS fictif est créé dans un dossier temporaire (effacé à la fin). L'indexation tourne dans un processus séparé
pour mesurer sa mémoire à part (pic - mémoire au démarrage). Dépendances de test : reportlab, python-docx, openpyxl, pypdf.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fabrique_nas as fab  # noqa: E402

RACINE = Path(__file__).resolve().parent.parent
MOTS = ("appel fonds echeancier acquereur lot batiment fondations hors eau achevement situation travaux planning reserve livraison "
        "bail loyer quittance charges indice irl facture banque notaire contrat avenant attestation avancement permis assurance "
        "maconnerie electricite plomberie menuiserie terrassement couverture peinture carrelage garantie retenue").split()


def phrase(rnd: random.Random, n: int) -> str:
    return " ".join(rnd.choice(MOTS) for _ in range(n))


def fabriquer(racine: Path, n: int) -> dict:
    rnd = random.Random(2026)
    repartition = {"txt": int(n * 0.60), "docx": int(n * 0.16), "xlsx": int(n * 0.14), "pdf": n - int(n * 0.60) - int(n * 0.16) - int(n * 0.14)}
    compteur = 0
    t0 = time.monotonic()
    for ext, nb in repartition.items():
        for i in range(nb):
            dossier = racine / f"Programme-{rnd.randint(1, 8)}" / f"Lot-{rnd.randint(1, 25):02d}" / rnd.choice(["acquereurs", "chantier", "compta", "juridique"])
            dossier.mkdir(parents=True, exist_ok=True)
            nom = dossier / f"doc-{compteur:05d}.{ext}"
            compteur += 1
            corps = "DONNÉES FICTIVES — TEST " + phrase(rnd, 120)
            if ext == "txt":
                nom.write_text(corps, encoding="utf-8")
            elif ext == "docx":
                fab.docx(nom, [corps, phrase(rnd, 60)], [["a", "b"], [phrase(rnd, 3), phrase(rnd, 3)]])
            elif ext == "xlsx":
                fab.xlsx(nom, {"Feuille": [[phrase(rnd, 2), rnd.randint(1, 100000), phrase(rnd, 3)] for _ in range(30)]})
            else:
                fab.pdf(nom, [corps[j:j + 90] for j in range(0, 540, 90)])
    return {"repartition": repartition, "generation_s": round(time.monotonic() - t0, 1)}


CODE = r'''
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
import documents as d
def rss(cle="VmRSS"):
    for l in open("/proc/self/status"):
        if l.startswith(cle): return int(l.split()[1]) // 1024
nas, index = sys.argv[2], sys.argv[3]
base = rss()
cfg = d.Config(racine=nas, index=index, pause=0, fuseau="Europe/Paris")
D = d.Documents(cfg)
sortie = {"memoire_depart_mo": base}
t = time.monotonic(); b = D.indexer(); sortie["premiere_indexation"] = {**b, "duree_s": round(time.monotonic() - t, 1)}
sortie["pic_memoire_mo"] = rss("VmHWM")      # (ru_maxrss hériterait du processus parent après exec)
sortie["memoire_apres_mo"] = rss()
t = time.monotonic(); b = D.indexer(); sortie["reprise_sans_changement"] = {**b, "duree_s": round(time.monotonic() - t, 1)}
# on modifie 50 fichiers puis on reprend
import pathlib
fichiers = sorted(p for p in pathlib.Path(nas).rglob("*.txt"))[:50]
for p in fichiers:
    p.write_text("DONNÉES FICTIVES — TEST modifié " + p.name, encoding="utf-8")
t = time.monotonic(); b = D.indexer(); sortie["reprise_apres_50_modifications"] = {**b, "duree_s": round(time.monotonic() - t, 2)}
requetes = ["appel fonds fondations", "bail loyer irl", "attestation avancement", "doc-04321", "situation travaux maconnerie", "licorne introuvable"]
temps = []
for _ in range(10):
    for q in requetes:
        t = time.monotonic(); D.executer("rechercher_documents", {"texte": q}, "mesure", "archiviste"); temps.append(time.monotonic() - t)
temps.sort()
sortie["recherche_ms"] = {"moyenne": round(1000 * sum(temps) / len(temps), 1), "p95": round(1000 * temps[int(len(temps) * 0.95)], 1), "max": round(1000 * temps[-1], 1)}
t = time.monotonic(); D.executer("lire_document", {"chemin": "Programme-1/Lot-01/acquereurs/" + (sorted(os.listdir(os.path.join(nas, "Programme-1/Lot-01/acquereurs")))[0])}, "m", "a"); sortie["lecture_ms"] = round(1000 * (time.monotonic() - t), 1)
sortie["taille_index_mo"] = round(os.path.getsize(index) / 1048576, 1)
print(json.dumps(sortie))
'''


def main():
    n = int(sys.argv[sys.argv.index("--fichiers") + 1]) if "--fichiers" in sys.argv else 5000
    tmp = Path(tempfile.mkdtemp(prefix="mesure-nas-"))
    try:
        print(f"Fabrication de {n} fichiers fictifs…", flush=True)
        info = fabriquer(tmp / "nas", n)
        print(json.dumps(info, ensure_ascii=False), flush=True)
        r = subprocess.run([sys.executable, "-I", "-c", CODE, str(RACINE / "orchestrateur"), str(tmp / "nas"), str(tmp / "index.sqlite")],
                           capture_output=True, text=True)
        if r.returncode:
            print(r.stderr[-2000:])
            raise SystemExit(1)
        res = json.loads(r.stdout.strip().splitlines()[-1])
        res["fichiers"] = n
        print(json.dumps(res, indent=2, ensure_ascii=False))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
