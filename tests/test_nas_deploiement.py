"""Outil « documents » : docker-compose, réglages, script de coupure d'urgence et procédure serveur."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import RACINE

ABI = RACINE / "instances" / "abi"


def charge(nom):
    return yaml.safe_load((ABI / nom).read_text(encoding="utf-8"))


# =============================================================== compose
def test_le_compose_de_base_ne_monte_aucun_dossier_du_serveur_et_le_nas_est_eteint():
    o = charge("docker-compose.yml")["services"]["abi_orchestrateur"]
    assert o["volumes"] == ["./agents:/app/agents:ro"]                       # aucun montage /nas dans la base
    assert o["environment"]["PK_NAS_ACTIF"] == "${ABI_NAS_ACTIF:-0}"        # éteint par défaut
    assert o["environment"]["PK_NAS_CHEMIN"] == "/nas"
    assert o["mem_limit"] == "256m"


def test_le_montage_du_nas_n_existe_que_dans_le_fichier_complementaire_et_en_lecture_seule():
    nas = charge("docker-compose.nas.yml")
    assert set(nas["services"]) == {"abi_orchestrateur"}
    volumes = nas["services"]["abi_orchestrateur"]["volumes"]
    montage = [v for v in volumes if v.endswith(":/nas:ro")]
    assert len(montage) == 1 and "${ABI_NAS_CHEMIN:?" in montage[0]            # chemin obligatoire, jamais par défaut
    assert not any(":/nas" in v and not v.endswith(":ro") for v in volumes)    # aucun montage /nas en écriture
    assert "abi_nas_index:/app/data" in volumes and nas["volumes"]["abi_nas_index"]["name"] == "abi_nas_index"
    assert "ports" not in nas["services"]["abi_orchestrateur"]
    assert nas["services"]["abi_orchestrateur"]["mem_limit"] == "384m"


def test_aucun_autre_service_ne_voit_le_nas():
    for fichier in ("docker-compose.yml", "docker-compose.nas.yml"):
        for nom, svc in charge(fichier)["services"].items():
            if nom != "abi_orchestrateur":
                assert not any("/nas" in str(v) for v in svc.get("volumes", [])), (fichier, nom)
    assert not any("/nas" in v for v in charge("docker-compose.yml")["services"]["abi_orchestrateur"]["volumes"])


def test_chat_transmet_l_utilisateur_pour_le_journal():
    assert charge("docker-compose.yml")["services"]["abi_openwebui"]["environment"]["ENABLE_FORWARD_USER_INFO_HEADERS"] == "true"


def test_variables_nas_documentees_sans_valeur():
    exemple = (ABI / ".env.exemple").read_text(encoding="utf-8")
    for var in ("ABI_NAS_ACTIF", "ABI_NAS_CHEMIN", "ABI_NAS_EXCLUS", "ABI_NAS_TAILLE_MAX_MO", "ABI_NAS_FICHIERS_MAX",
                "ABI_NAS_CARACTERES_MAX", "ABI_NAS_DELAI", "ABI_NAS_NUIT", "ABI_NAS_MODE"):
        assert re.search(rf"^{var}=$", exemple, re.M), var
    texte = (ABI / "docker-compose.yml").read_text(encoding="utf-8") + (ABI / "docker-compose.nas.yml").read_text(encoding="utf-8")
    assert set(re.findall(r"\$\{(ABI_[A-Z_]+)", texte)) <= set(re.findall(r"^(ABI_[A-Z_]+)=", exemple, re.M))
    assert not re.search(r"(password|identifiant|secret)[ \t]*=[ \t]*\S", exemple, re.I)                   # aucun identifiant NAS dans le dépôt


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker absent")
def test_compose_fusionne_valide_et_montage_en_lecture_seule():
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp", "ABI_LITELLM_KEY": "x", "ABI_API_KEY": "x", "ABI_ADMIN_KEY": "x",
           "ABI_POSTGRES_PASSWORD": "x", "ABI_WEBUI_SECRET_KEY": "x", "ABI_NAS_CHEMIN": "/mnt/nas-abi"}
    r = subprocess.run(["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.nas.yml", "config"], cwd=ABI,
                       capture_output=True, text=True, env=env)
    if "not a docker command" in r.stderr:
        pytest.skip("plugin compose absent")
    assert r.returncode == 0, r.stderr
    rendu = yaml.safe_load(r.stdout)["services"]["abi_orchestrateur"]
    montages = {v["target"]: v for v in rendu["volumes"]}
    assert montages["/nas"]["read_only"] is True and montages["/nas"]["source"] == "/mnt/nas-abi"
    assert montages["/app/agents"]["read_only"] is True
    sans = subprocess.run(["docker", "compose", "-f", "docker-compose.yml", "config"], cwd=ABI, capture_output=True, text=True,
                          env={k: v for k, v in env.items() if k != "ABI_NAS_CHEMIN"})
    assert "/nas" not in [v["target"] for v in yaml.safe_load(sans.stdout)["services"]["abi_orchestrateur"]["volumes"]]


def test_image_orchestrateur_contient_l_outil_et_la_dependance_epinglee():
    dockerfile = (RACINE / "orchestrateur" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY app.py documents.py" in dockerfile and "/app/data" in dockerfile
    assert re.search(r"^pypdf==\d+\.\d+\.\d+$", (RACINE / "orchestrateur" / "requirements.txt").read_text(encoding="utf-8"), re.M)
    assert (RACINE / "orchestrateur" / "documents.py").exists()


# =============================================================== coupure d'urgence
def preparer(tmp_path, avec_fstab=True, avec_env=True, monte=True):
    dossier = tmp_path / "abi"
    dossier.mkdir()
    shutil.copy(ABI / "nas-coupure-urgence.sh", dossier / "nas-coupure-urgence.sh")
    if avec_env:
        (dossier / ".env").write_text("ABI_API_KEY=abc\nABI_NAS_ACTIF=1\nABI_NAS_CHEMIN=/mnt/nas-abi\nCOMPOSE_FILE=docker-compose.yml:docker-compose.nas.yml\nABI_ADMIN_KEY=def\n")
    montage = "/mnt/nas-abi"
    fstab = tmp_path / "fstab"
    fstab.write_text("UUID=1234 / ext4 defaults 0 1\n" + (f"//100.1.2.3/PARTAGE {montage} cifs credentials=/root/x,ro,nofail 0 0\n" if avec_fstab else ""))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    journal = tmp_path / "appels.log"
    for nom in ("docker", "umount", "tailscale", "systemctl"):
        f = bin_ / nom
        f.write_text(f'#!/bin/sh\necho "{nom} $@" >> {journal}\n')
        f.chmod(0o755)
    f = bin_ / "mountpoint"
    f.write_text(f'#!/bin/sh\n{"exit 0" if monte else "exit 1"}\n')
    f.chmod(0o755)
    env = {"PATH": f"{bin_}:{os.environ['PATH']}", "ABI_NAS_MONTAGE": montage, "ABI_FSTAB": str(fstab), "COMPOSE_FILE": "ne-doit-pas-servir"}
    return dossier, fstab, journal, env


def lancer(dossier, env, *args):
    return subprocess.run(["bash", str(dossier / "nas-coupure-urgence.sh"), *args], capture_output=True, text=True, env=env, cwd="/")


def test_coupure_d_urgence_en_une_commande(tmp_path):
    dossier, fstab, journal, env = preparer(tmp_path)
    r = lancer(dossier, env)
    assert r.returncode == 0, r.stdout + r.stderr
    envfile = (dossier / ".env").read_text()
    assert "ABI_NAS_ACTIF=0" in envfile and "COMPOSE_FILE" not in envfile
    assert "ABI_API_KEY=abc" in envfile and "ABI_ADMIN_KEY=def" in envfile                       # le reste de .env est intact
    appels = journal.read_text().splitlines()
    assert "docker compose -p abi --project-directory . up -d --no-deps --force-recreate abi_orchestrateur" in appels          # seulement l'orchestrateur
    assert "umount /mnt/nas-abi" in appels
    assert appels.index("docker compose -p abi --project-directory . up -d --no-deps --force-recreate abi_orchestrateur") < appels.index("umount /mnt/nas-abi")
    assert any(l.startswith("# [coupure d'urgence] //100.1.2.3/PARTAGE") for l in fstab.read_text().splitlines())
    assert "UUID=1234 / ext4 defaults 0 1" in fstab.read_text().splitlines()                      # le reste de fstab est intact
    assert not any(l.startswith("tailscale") for l in appels)
    assert "✔ Accès au NAS coupé" in r.stdout


def test_coupure_d_urgence_avec_tailscale_et_rejouable(tmp_path):
    dossier, fstab, journal, env = preparer(tmp_path)
    assert lancer(dossier, env, "--tailscale").returncode == 0
    assert "tailscale down" in journal.read_text()
    avant = fstab.read_text()
    assert lancer(dossier, env).returncode == 0                      # rejouable : la ligne n'est pas commentée deux fois
    assert fstab.read_text() == avant


def test_coupure_d_urgence_sans_env_ni_fstab_ni_montage(tmp_path):
    dossier, fstab, journal, env = preparer(tmp_path, avec_fstab=False, avec_env=False, monte=False)
    r = lancer(dossier, env)
    assert r.returncode == 0 and "pas de fichier .env" in r.stdout and "n'était pas monté" in r.stdout
    assert "umount" not in journal.read_text()


def test_coupure_d_urgence_signale_un_echec_sans_s_arreter(tmp_path):
    dossier, fstab, journal, env = preparer(tmp_path)
    echec = Path(env["PATH"].split(":")[0]) / "umount"
    echec.write_text("#!/bin/sh\nexit 1\n")
    r = lancer(dossier, env)
    assert r.returncode == 1 and "démontage impossible" in r.stdout
    assert "ABI_NAS_ACTIF=0" in (dossier / ".env").read_text()      # l'essentiel (désactivation) est fait malgré tout


def test_script_de_coupure_executable_et_sans_commande_destructrice():
    f = ABI / "nas-coupure-urgence.sh"
    assert f.stat().st_mode & stat.S_IXUSR
    texte = f.read_text(encoding="utf-8")
    assert not re.search(r"rm\s+-rf|docker\s+volume\s+rm|down\s+-v|docker\s+system\s+prune|shred|mkfs", texte)
    assert subprocess.run(["bash", "-n", str(f)]).returncode == 0
    if shutil.which("shellcheck"):
        assert subprocess.run(["shellcheck", "-S", "warning", str(f)], capture_output=True).returncode == 0


# =============================================================== procédure serveur
def test_procedure_serveur_complete():
    t = (ABI / "NAS-LISEZMOI.md").read_text(encoding="utf-8")
    for mot in ("Tailscale", "Synology", "QNAP", "bureau-ia", "Lecture seule", "cifs-utils", "/root/.nas-abi-identifiants", "600",
                "/etc/fstab", "ro,", "ABI_NAS_ACTIF", "dc up -d --no-deps abi_orchestrateur", "nas-coupure-urgence.sh",
                "COUPURE D'URGENCE", "Désinstallation complète", "terminal Hostinger", "touch /mnt/nas-abi/essai.txt", "tailscale up",
                "Disable key expiry", "SMB 3", "docker volume rm abi_nas_index", "tailscale logout"):
        assert mot in t, mot
    assert t.count("*(terminal Hostinger)*") >= 6 and "*(sur le NAS" in t
    # aucun mot de passe écrit en clair dans la procédure
    assert not re.search(r"password=(?!%s)\S+", t)


def test_procedure_ne_met_aucun_identifiant_dans_le_depot():
    for chemin in ABI.rglob("*"):
        if chemin.is_file() and chemin.suffix in (".md", ".yml", ".sh", ".exemple", ".conf"):
            texte = chemin.read_text(encoding="utf-8", errors="ignore")
            assert not re.search(r"^\s*password=\S", texte, re.M), chemin
    assert not (ABI / ".env").exists()
