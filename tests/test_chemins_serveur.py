"""Chemins et commandes de la procédure serveur ABI : le dépôt est cloné dans /opt/abi-bureau-ia, la pile tourne sous le projet Compose « abi ».

Ces tests empêchent la procédure, et surtout la COUPURE D'URGENCE du NAS, de citer un dossier qui n'existe pas ou de viser un autre projet Compose.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import RACINE

ABI = RACINE / "instances" / "abi"
DOCS = [ABI / "LISEZMOI.md", ABI / "NAS-LISEZMOI.md", ABI / "STANDARD-LISEZMOI.md"]
SCRIPT = ABI / "nas-coupure-urgence.sh"
DEPOT = "/opt/abi-bureau-ia"
DC = 'dc() { (cd /opt/abi-bureau-ia/instances/abi && docker compose -p abi --project-directory . "$@"); }'
CREES_A_L_INSTALLATION = {"instances/abi/.env"}                         # n'existe pas dans le dépôt : créé par la procédure


def fichiers_textes():
    return [p for p in ABI.rglob("*") if p.is_file() and p.suffix in (".md", ".sh", ".yml", ".exemple", ".conf") and "__pycache__" not in p.parts]


def test_plus_aucune_trace_de_l_ancien_dossier():
    for p in fichiers_textes():
        assert "pk-abi" not in p.read_text(encoding="utf-8"), p


def test_tous_les_chemins_cites_existent_dans_le_depot():
    """Chaque /opt/abi-bureau-ia/… cité (docs, script) correspond à un fichier ou dossier réel du dépôt."""
    vus = 0
    for p in DOCS + [SCRIPT]:
        for m in re.finditer(r"/opt/abi-bureau-ia((?:/[A-Za-z0-9_.\-]+)*)", p.read_text(encoding="utf-8")):
            rel = m.group(1).lstrip("/")
            vus += 1
            if rel and rel not in CREES_A_L_INSTALLATION:
                assert (RACINE / rel).exists(), f"{p.name} cite {m.group(0)} : {rel} n'existe pas dans le dépôt"
    assert vus >= 15
    # les autres dossiers cités sous /opt sont ceux du reste du serveur, jamais l'ancien nom
    for p in DOCS + [SCRIPT]:
        for m in re.finditer(r"/opt/([A-Za-z0-9_.\-]+)", p.read_text(encoding="utf-8")):
            assert m.group(1) in ("abi-bureau-ia", "pixelkreol-bureau-ia", "pk-sauvegardes"), (p.name, m.group(0))


def test_la_procedure_de_coupure_cite_le_vrai_script():
    for p in (ABI / "NAS-LISEZMOI.md", SCRIPT):
        assert f"bash {DEPOT}/instances/abi/nas-coupure-urgence.sh" in p.read_text(encoding="utf-8"), p
    assert SCRIPT.exists() and os.access(SCRIPT, os.X_OK)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_commandes_compose_via_dc(doc):
    t = doc.read_text(encoding="utf-8")
    assert t.count(DC) == 1, "la fonction dc doit être définie une fois, à l'identique"
    avant = t[: t.index(DC)]
    reste = t.replace(DC, "")
    assert "docker compose" not in re.sub(r"`dc` remplace `docker compose`|Docker Compose|`docker compose`", "", reste)
    first_use = re.search(r"^\s*dc [a-z]", t, re.M)
    if first_use:
        assert first_use.start() > t.index(DC), f"{doc.name} : dc utilisée avant sa définition"
    assert "pk-abi" not in t and avant is not None


# =============================================================== le script de coupure, exécuté à blanc (faux docker/umount/…)
def installer(tmp_path, env_texte):
    inst = tmp_path / "abi-bureau-ia" / "instances" / "abi"
    inst.mkdir(parents=True)
    shutil.copy(SCRIPT, inst / SCRIPT.name)
    if env_texte is not None:
        (inst / ".env").write_text(env_texte)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    journal = tmp_path / "appels.log"
    for nom in ("docker", "umount", "tailscale", "systemctl"):
        f = bin_ / nom
        f.write_text(f'#!/bin/sh\necho "{nom} dans=$(pwd) args=$*" >> {journal}\n')
        f.chmod(0o755)
    f = bin_ / "mountpoint"
    f.write_text("#!/bin/sh\nexit 1\n")
    f.chmod(0o755)
    fstab = tmp_path / "fstab"
    fstab.write_text("UUID=1 / ext4 defaults 0 1\n")
    env = {"PATH": f"{bin_}:{os.environ['PATH']}", "ABI_FSTAB": str(fstab), "ABI_NAS_MONTAGE": str(tmp_path / "montage-inexistant")}
    return inst, journal, env


def lancer(inst, env, cwd="/", *args):
    return subprocess.run(["bash", str(inst / SCRIPT.name), *args], capture_output=True, text=True, env=env, cwd=cwd)


def test_coupure_travaille_dans_le_dossier_de_l_instance_et_vise_le_projet_abi(tmp_path):
    inst, journal, env = installer(tmp_path, "ABI_NAS_ACTIF=1\nCOMPOSE_FILE=docker-compose.yml:docker-compose.nas.yml\n")
    r = lancer(inst, env, cwd="/tmp")                                  # lancée d'ailleurs : elle retrouve son propre dossier
    assert r.returncode == 0, r.stdout + r.stderr
    appel = journal.read_text().splitlines()[0]
    assert appel == f"docker dans={inst} args=compose -p abi --project-directory . up -d --no-deps --force-recreate abi_orchestrateur"
    assert (inst / ".env").read_text() == "ABI_NAS_ACTIF=0\n"          # plus de COMPOSE_FILE s'il ne restait que le NAS
    # le nom du projet est réglable (une autre installation), jamais perdu
    inst2, journal2, env2 = installer(tmp_path / "autre", "ABI_NAS_ACTIF=1\n")
    assert lancer(inst2, {**env2, "ABI_COMPOSE_PROJET": "abi-test"}).returncode == 0
    assert " -p abi-test " in " " + journal2.read_text().splitlines()[0].split("args=")[1] + " "


def test_coupure_garde_le_standard_telephonique_dans_compose_file(tmp_path):
    inst, journal, env = installer(tmp_path, "ABI_NAS_ACTIF=1\nCOMPOSE_FILE=docker-compose.yml:docker-compose.nas.yml:docker-compose.standard.yml\nABI_X=1\n")
    assert lancer(inst, env).returncode == 0
    assert (inst / ".env").read_text() == "ABI_NAS_ACTIF=0\nCOMPOSE_FILE=docker-compose.yml:docker-compose.standard.yml\nABI_X=1\n"
    inst, journal, env = installer(tmp_path / "bis", "COMPOSE_FILE=docker-compose.yml:docker-compose.standard.yml:docker-compose.nas.yml\n")
    assert lancer(inst, env).returncode == 0
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.standard.yml\n" in (inst / ".env").read_text()


def test_coupure_a_blanc_sur_l_arborescence_du_depot(tmp_path):
    """Le script réel, placé dans une copie de l'arborescence du dépôt à l'emplacement documenté : le dossier où il travaille contient les fichiers compose."""
    depot = tmp_path / "abi-bureau-ia"
    shutil.copytree(ABI, depot / "instances" / "abi", ignore=shutil.ignore_patterns("standard", "donnees-fictives", "__pycache__"))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    journal = tmp_path / "appels.log"
    for nom in ("docker", "umount", "systemctl"):
        f = bin_ / nom
        f.write_text(f'#!/bin/sh\necho "{nom} $(pwd)" >> {journal}\n')
        f.chmod(0o755)
    env = {"PATH": f"{bin_}:{os.environ['PATH']}", "ABI_FSTAB": str(tmp_path / "pas-de-fstab"), "ABI_NAS_MONTAGE": str(tmp_path / "pas-monte")}
    r = subprocess.run(["bash", str(depot / "instances" / "abi" / "nas-coupure-urgence.sh")], capture_output=True, text=True, env=env, cwd="/")
    assert r.returncode == 0, r.stdout + r.stderr
    dossier = journal.read_text().split()[1]
    assert Path(dossier, "docker-compose.yml").exists() and Path(dossier, "docker-compose.nas.yml").exists()
    assert dossier.endswith("abi-bureau-ia/instances/abi")


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker absent")
def test_dc_des_trois_documents_resout_le_projet_abi(tmp_path):
    """La fonction dc de chaque document, exécutée telle quelle, vise le projet « abi » et lit COMPOSE_FILE du .env de instances/abi."""
    depot = tmp_path / "abi-bureau-ia"
    inst = depot / "instances" / "abi"
    inst.mkdir(parents=True)
    for f in ("docker-compose.yml", "docker-compose.nas.yml", "docker-compose.standard.yml"):
        shutil.copy(ABI / f, inst / f)
    (inst / "standard").symlink_to(ABI / "standard")
    (inst / ".env").write_text("ABI_LITELLM_KEY=x\nABI_API_KEY=x\nABI_ADMIN_KEY=x\nABI_POSTGRES_PASSWORD=x\nABI_WEBUI_SECRET_KEY=x\nABI_NAS_CHEMIN=/tmp\n"
                               "COMPOSE_FILE=docker-compose.yml:docker-compose.nas.yml:docker-compose.standard.yml\n")
    for doc in DOCS:
        definition = re.search(r"^dc\(\) \{.*\}$", doc.read_text(encoding="utf-8"), re.M).group(0).replace(DEPOT, str(depot))
        r = subprocess.run(["bash", "-c", definition + '\ndc config'], capture_output=True, text=True, cwd=tmp_path,
                           env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp"})
        if "not a docker command" in r.stderr:
            pytest.skip("plugin compose absent")
        assert r.returncode == 0, (doc.name, r.stderr)
        assert r.stdout.startswith("name: abi\n") and "abi_standard:" in r.stdout and "target: /nas" in r.stdout
