"""G/H. Scripts : install.sh, verifier.sh, outils/*.py (sans Docker : on simule la commande docker)."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import RACINE

OUTILS = RACINE / "outils"


# ---------------------------------------------------------------- patch_openwebui.py
def base_recente(chemin: Path, urls=None):
    con = sqlite3.connect(chemin)
    con.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)")
    con.execute("CREATE TABLE chat (id TEXT, titre TEXT)")
    con.execute("INSERT INTO chat VALUES ('c1', 'Mon historique précieux')")
    for k, v in (("openai.api_base_urls", urls or ["https://api.exemple.test/v1", "http://pk_litellm:4000/v1"]),
                 ("openai.api_keys", ["cle-perso", "ancienne"]), ("openai.api_configs", {"0": {"enable": True}})):
        con.execute("INSERT INTO config VALUES (?,?,0)", (k, json.dumps(v)))
    con.commit()
    con.close()


def lancer_patch(db: Path):
    return subprocess.run([sys.executable, "-I", str(OUTILS / "patch_openwebui.py"), str(db),
                           "http://pk_orchestrateur:4001/v1", "cle-orch", "http://pk_litellm:4000/v1", "cle-llm"],
                          capture_output=True, text=True)


def test_patch_openwebui_garde_les_autres_connexions_et_l_historique(tmp_path):
    db = tmp_path / "webui.db"
    base_recente(db)
    r = lancer_patch(db)
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    urls = json.loads(con.execute("SELECT value FROM config WHERE key='openai.api_base_urls'").fetchone()[0])
    assert urls == ["http://pk_orchestrateur:4001/v1", "http://pk_litellm:4000/v1", "https://api.exemple.test/v1"]
    assert con.execute("SELECT titre FROM chat").fetchone()[0] == "Mon historique précieux"


def test_patch_openwebui_sauvegarde_la_base_avant_de_la_modifier(tmp_path):
    db = tmp_path / "webui.db"
    base_recente(db)
    avant = sqlite3.connect(db).execute("SELECT value FROM config WHERE key='openai.api_keys'").fetchone()[0]
    assert lancer_patch(db).returncode == 0
    copies = sorted(tmp_path.glob("webui.db.avant-*"))
    assert len(copies) == 1, list(tmp_path.iterdir())
    ancienne = sqlite3.connect(copies[0]).execute("SELECT value FROM config WHERE key='openai.api_keys'").fetchone()[0]
    assert ancienne == avant
    assert stat.S_IMODE(copies[0].stat().st_mode) & 0o077 == 0     # lisible par son propriétaire seulement


def test_patch_openwebui_ne_garde_que_les_3_dernieres_copies(tmp_path):
    db = tmp_path / "webui.db"
    base_recente(db)
    for i in range(5):
        (tmp_path / f"webui.db.avant-2026010{i}-000000").write_bytes(b"ancienne copie")
    assert lancer_patch(db).returncode == 0
    restantes = sorted(p.name for p in tmp_path.glob("webui.db.avant-*"))
    assert len(restantes) == 3 and restantes[0] == "webui.db.avant-20260103-000000"


def test_patch_openwebui_sans_rien_a_faire_ne_cree_pas_de_copie(tmp_path):
    db = tmp_path / "webui.db"
    sqlite3.connect(db).execute("CREATE TABLE config (key TEXT, value TEXT, updated_at INTEGER)").connection.commit()
    assert lancer_patch(db).returncode == 3
    assert not list(tmp_path.glob("webui.db.avant-*"))
    assert lancer_patch(tmp_path / "absente.db").returncode == 3


def test_patch_openwebui_signale_une_connexion_illisible(tmp_path):
    db = tmp_path / "webui.db"
    base_recente(db)
    con = sqlite3.connect(db)
    con.execute("UPDATE config SET value='{pas du json' WHERE key='openai.api_configs'")
    con.commit()
    con.close()
    r = lancer_patch(db)
    assert r.returncode == 0
    assert "illisible" in (r.stdout + r.stderr).lower()                # plus de remise à zéro silencieuse


# ---------------------------------------------------------------- generer_litellm.py
def charge_generateur():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen_litellm", OUTILS / "generer_litellm.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generateur_n_active_que_les_fournisseurs_avec_cle():
    gen = charge_generateur()
    cfg = yaml.safe_load((RACINE / "agents" / "cerveaux.yaml").read_text(encoding="utf-8"))
    conf, rapport = gen.construire(cfg, {"GROQ_API_KEY": "x", "OPENROUTER_API_KEY": "y"})
    noms = [m["litellm_params"]["model"] for m in conf["model_list"]]
    assert all(n.startswith(("groq/", "openrouter/")) for n in noms)
    assert all(m["litellm_params"]["api_key"].startswith("os.environ/") for m in conf["model_list"])   # jamais de clé écrite
    assert conf["router_settings"]["num_retries"] == 0                                                # pas de double appel
    assert set(rapport) == {"pk-rapide", "pk-costaud", "pk-code", "pk-vision", "pk-long"}


def test_generateur_ne_met_aucune_cle_dans_le_fichier(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=valeur-secrete-de-test\n", encoding="utf-8")
    sortie = tmp_path / "litellm.yaml"
    r = subprocess.run([sys.executable, "-I", str(OUTILS / "generer_litellm.py"), "--env", str(env), "--sortie", str(sortie)],
                       capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert r.returncode == 0, r.stderr
    assert "valeur-secrete-de-test" not in sortie.read_text(encoding="utf-8")


def test_generateur_sans_aucune_cle_echoue_clairement(tmp_path):
    r = subprocess.run([sys.executable, "-I", str(OUTILS / "generer_litellm.py"), "--env", str(tmp_path / "vide.env"),
                        "--sortie", str(tmp_path / "x.yaml")], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert r.returncode == 2 and "aucune clé" in r.stderr.lower()


# ---------------------------------------------------------------- verifier.sh
def faux_docker(dossier: Path):
    """Un « docker » qui répond que tout va bien, sans lancer quoi que ce soit."""
    bin_ = dossier / "bin"
    bin_.mkdir()
    for nom, code in (("docker", "echo healthy"), ("curl", "echo 200")):
        f = bin_ / nom
        f.write_text(f"#!/bin/sh\n{code}\n")
        f.chmod(0o755)
    return bin_


def test_verifier_sh_n_execute_pas_le_contenu_de_env(tmp_path):
    """`source .env` exécutait n'importe quelle commande présente dans le fichier."""
    dossier = tmp_path / "bureau"
    (dossier / "outils").mkdir(parents=True)
    shutil.copy(RACINE / "verifier.sh", dossier / "verifier.sh")
    shutil.copy(OUTILS / "verifier.py", dossier / "outils" / "verifier.py")
    piege = tmp_path / "PIRATE"
    (dossier / ".env").write_text(f"PK_DOMAINE_AGENTS=agents.test.invalid\nX=$(touch {piege})\nY=`touch {piege}`\n"
                                  f"WEBUI_NAME=Bureau IA avec espaces\n")
    env = {"PATH": f"{faux_docker(tmp_path)}:{os.environ['PATH']}"}
    subprocess.run(["bash", str(dossier / "verifier.sh")], capture_output=True, text=True, env=env, timeout=60)
    assert not piege.exists(), "le contenu de .env a été exécuté comme une commande"


def test_verifier_sh_lit_les_domaines_du_env(tmp_path):
    dossier = tmp_path / "bureau"
    (dossier / "outils").mkdir(parents=True)
    shutil.copy(RACINE / "verifier.sh", dossier / "verifier.sh")
    shutil.copy(OUTILS / "verifier.py", dossier / "outils" / "verifier.py")
    (dossier / ".env").write_text('PK_DOMAINE_AGENTS="agents.test.invalid"\nPK_DOMAINE_N8N=n8n.test.invalid\n')
    env = {"PATH": f"{faux_docker(tmp_path)}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", str(dossier / "verifier.sh")], capture_output=True, text=True, env=env, timeout=60)
    assert "https://agents.test.invalid" in r.stdout and "https://n8n.test.invalid" in r.stdout


# ---------------------------------------------------------------- relecture statique de install.sh / verifier.sh
SCRIPTS = ["install.sh", "verifier.sh", "scripts/restaurer.sh", "scripts/initdb/01-bases.sh"]


@pytest.mark.parametrize("nom", SCRIPTS)
def test_syntaxe_bash(nom):
    assert subprocess.run(["bash", "-n", str(RACINE / nom)], capture_output=True).returncode == 0


@pytest.mark.parametrize("nom", SCRIPTS)
def test_aucune_commande_destructrice(nom):
    texte = (RACINE / nom).read_text(encoding="utf-8")
    for interdit in (r"docker\s+system\s+prune", r"docker\s+volume\s+(rm|prune)", r"docker\s+(compose\s+)?down\b[^\n]*\s(-v|--volumes)\b",
                     r"docker\s+rm\b[^\n]*\s-[a-z]*v", r"rm\s+-rf\s+/(\s|$)", r"rm\s+-rf\s+\"?\$\{?DEST\}?\"?(\s|$)",
                     r"DROP\s+(DATABASE|TABLE)", r"TRUNCATE", r"mkfs", r">\s*\"?\$DEST/\.env\"?\s*$"):
        assert not re.search(interdit, texte, re.I | re.M), f"{nom} contient : {interdit}"


def test_install_sh_sauvegarde_avant_d_ecraser_env():
    texte = (RACINE / "install.sh").read_text(encoding="utf-8")
    assert texte.index('cp "$DEST/.env" "$SAUV/env-$HORO"') < texte.index('mv "$DEST/.env.nouveau" "$DEST/.env"')
    assert "set -Eeuo pipefail" in texte
    assert re.search(r'\[\[ \$EUID -eq 0 \]\]', texte)


def test_install_sh_sauvegarde_la_base_avant_toute_modification_de_openwebui():
    texte = (RACINE / "install.sh").read_text(encoding="utf-8")
    assert texte.index("Sauvegarde de l'existant") < texte.index("patch_openwebui.py")
    assert "pg_dumpall" in texte


def test_install_sh_ne_publie_aucun_port_en_dehors_de_caddy():
    texte = (RACINE / "install.sh").read_text(encoding="utf-8")
    assert not re.search(r'-p\s+\d+:\d+|--publish', texte)


def test_chemins_en_dur_sont_modifiables():
    for nom in ("install.sh", "scripts/restaurer.sh"):
        texte = (RACINE / nom).read_text(encoding="utf-8")
        assert "${PK_DEST:-" in texte and "${PK_SAUVEGARDES:-" in texte


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck absent")
@pytest.mark.parametrize("nom", SCRIPTS)
def test_shellcheck_sans_avertissement(nom):
    r = subprocess.run(["shellcheck", "-S", "warning", str(RACINE / nom)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


# ---------------------------------------------------------------- scripts/restaurer.sh (retour arrière)
def preparer_restauration(tmp_path):
    """Un faux serveur : dossier installé, archive de sauvegarde, et un « docker » qui note ses appels."""
    parent = tmp_path / "opt"
    dest = parent / "pixelkreol-bureau-ia"
    (dest / "orchestrateur").mkdir(parents=True)
    (dest / "orchestrateur" / "app.py").write_text("VERSION = 'ancienne'\n")
    sauv = tmp_path / "sauvegardes"
    sauv.mkdir()
    subprocess.run(["tar", "-czf", str(sauv / "bureau-ia-fichiers-20261010-120000.tgz"), "-C", str(parent),
                    "pixelkreol-bureau-ia"], check=True)
    (dest / "orchestrateur" / "app.py").write_text("VERSION = 'nouvelle'\n")
    journal = tmp_path / "docker.log"
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    docker = bin_ / "docker"
    docker.write_text(f'#!/bin/sh\necho "$@" >> {journal}\n')
    docker.chmod(0o755)
    env = {"PATH": f"{bin_}:{os.environ['PATH']}", "PK_DEST": str(dest), "PK_SAUVEGARDES": str(sauv)}
    return dest, sauv, journal, env


def test_restaurer_remet_l_ancien_code_ET_reconstruit_l_image(tmp_path):
    dest, sauv, journal, env = preparer_restauration(tmp_path)
    r = subprocess.run(["bash", str(RACINE / "scripts" / "restaurer.sh"), "20261010-120000"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "ancienne" in (dest / "orchestrateur" / "app.py").read_text()
    assert list(dest.parent.glob("pixelkreol-bureau-ia.v2-*"))                   # la version récente est gardée à côté
    assert "compose up -d --build" in journal.read_text()                      # sinon l'ancien code ne tournerait pas


def test_restaurer_refuse_une_archive_abimee_sans_rien_deplacer(tmp_path):
    dest, sauv, journal, env = preparer_restauration(tmp_path)
    (sauv / "bureau-ia-fichiers-20261010-130000.tgz").write_bytes(b"ce n'est pas une archive")
    r = subprocess.run(["bash", str(RACINE / "scripts" / "restaurer.sh"), "20261010-130000"], capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "illisible" in r.stdout
    assert "nouvelle" in (dest / "orchestrateur" / "app.py").read_text()        # le dossier en service n'a pas bougé
    assert not list(dest.parent.glob("pixelkreol-bureau-ia.v2-*"))
    assert not journal.exists()                                                 # docker n'a même pas été arrêté


def test_restaurer_sans_argument_liste_les_sauvegardes(tmp_path):
    dest, sauv, journal, env = preparer_restauration(tmp_path)
    r = subprocess.run(["bash", str(RACINE / "scripts" / "restaurer.sh")], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "20261010-120000" in r.stdout
