#!/usr/bin/env python3
"""pk-hebergeur — service hôte de PK Atelier (tourne en root sur le VPS, déclenché par systemd).

Lit les demandes JSON déposées par le conteneur pk_atelier dans /opt/pk-atelier/data/hebergement/demandes,
les valide strictement, puis :
  ajouter : crée /etc/nginx/sites-available/pk-site-<domaine> → proxy vers PK Atelier, recharge nginx,
            obtient le certificat Let's Encrypt (certbot --nginx) dès que le DNS pointe vers le VPS ;
  retirer : supprime la configuration et le certificat.
Écrit l'état dans hebergement/etat/<domaine>.json (lu par le conteneur).

Garde-fous : nom de domaine validé par expression régulière, jamais de domaine déjà servi par un autre
fichier nginx (agents, site, n8n…), seuls les fichiers pk-site-* sont gérés, aucun lien symbolique suivi,
nombre de sites plafonné, tentatives Let's Encrypt limitées (quotas).
Standard library uniquement. Usage : pk-hebergeur [--tout]   (--tout : traite aussi la file d'attente DNS)
"""
import fcntl
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

RACINE = Path(os.getenv("PK_DIR", "/opt/pk-atelier"))
H = RACINE / "data" / "hebergement"
DEM, ATT, ETAT = H / "demandes", H / "attente", H / "etat"
NGINX_DISPO = Path(os.getenv("PK_NGINX_DISPO", "/etc/nginx/sites-available"))
NGINX_ACTIFS = Path(os.getenv("PK_NGINX_ACTIFS", "/etc/nginx/sites-enabled"))
LE_LIVE = Path(os.getenv("PK_LE_LIVE", "/etc/letsencrypt/live"))
PORT = int(os.getenv("PK_PORT", "20300"))
UID = 10001
MAX_SITES = int(os.getenv("PK_MAX_SITES", "300"))
MAX_ESSAIS_CERT = 5
DELAI_ABANDON = 7 * 86400
SIMULATION = os.getenv("PK_SIMULATION") == "1"   # tests : pas de nginx ni certbot réels
RE_DOMAINE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def journal(msg):
    print(f"[pk-hebergeur] {msg}", flush=True)


def lire_env():
    env = {}
    f = RACINE / ".env"
    if f.is_file():
        for l in f.read_text().splitlines():
            if "=" in l and not l.lstrip().startswith("#"):
                k, v = l.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = lire_env()
IP_VPS = os.getenv("PK_IP", ENV.get("ATELIER_IP_VPS", ""))
HOTE_DEMO = ENV.get("ATELIER_HOTE_DEMO", "")


def verifier_dossiers():
    """Refuse de travailler si le conteneur a remplacé un dossier par un lien symbolique."""
    for d in (H, DEM, ATT, ETAT):
        st = os.lstat(d)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise SystemExit(f"{d} n'est pas un vrai dossier : arrêt par sécurité")


def lire_json(chemin: Path):
    fd = os.open(chemin, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        data = f.read(4096)
    return json.loads(data)


def ecrire_json(dossier: Path, nom: str, obj: dict):
    tmp = dossier / f".{nom}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    if os.geteuid() == 0:
        os.chown(tmp, UID, UID)
    os.replace(tmp, dossier / nom)


def ecrire_etat(domaine: str, etat: str, **kw):
    ancien = {}
    try:
        ancien = lire_json(ETAT / f"{domaine}.json")
    except (OSError, ValueError):
        pass
    obj = {"domaine": domaine, "etat": etat, "https": kw.pop("https", False),
           "maj": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "essais_cert": kw.pop("essais_cert", ancien.get("essais_cert", 0)), **kw}
    ecrire_json(ETAT, f"{domaine}.json", obj)
    journal(f"{domaine} → {etat} {kw.get('message', '')}")
    return obj


def ips(domaine: str) -> list:
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(domaine, None, socket.AF_INET)})
    except OSError:
        return []


def domaines_proteges() -> set:
    """Tous les server_name des fichiers nginx qui ne sont pas gérés par pk-hebergeur."""
    noms = set()
    for d in (NGINX_ACTIFS, NGINX_DISPO, Path("/etc/nginx/conf.d")):
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.name.startswith("pk-site-") or not f.is_file():
                continue
            try:
                txt = f.read_text(errors="ignore")
            except OSError:
                continue
            for m in re.finditer(r"server_name\s+([^;]+);", txt):
                noms.update(x.lower() for x in m.group(1).split())
    return noms


def geres() -> list:
    return [f.name[len("pk-site-"):] for f in NGINX_DISPO.glob("pk-site-*")] if NGINX_DISPO.is_dir() else []


def lancer(cmd: list, timeout=180):
    if SIMULATION:
        journal("SIMULATION : " + " ".join(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ipv6_dispo() -> bool:
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
        return True
    except OSError:
        return False


def conf_nginx(noms: list) -> str:
    v6 = "\n    listen [::]:80;" if ipv6_dispo() else ""
    return f"""# Géré par pk-hebergeur (PK Atelier) — ne pas modifier à la main
server {{
    listen 80;{v6}
    server_name {' '.join(noms)};
    client_max_body_size 20m;
    location /.well-known/acme-challenge/ {{ root /var/www/html; }}
    location /api/ {{ return 404; }}
    location /valider {{ return 404; }}
    location /f/ {{ return 404; }}
    location / {{
        proxy_pass http://127.0.0.1:{PORT};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60;
    }}
}}
"""


def recharger_nginx() -> tuple:
    t = lancer(["nginx", "-t"])
    if t.returncode != 0:
        return False, (t.stderr or t.stdout)[-400:]
    r = lancer(["systemctl", "reload", "nginx"])
    return r.returncode == 0, (r.stderr or "")[-400:]


def ajouter(dem: dict, source: Path):
    d, type_ = dem["domaine"], dem.get("type", "demo")
    if type_ == "demo" and not (HOTE_DEMO and d.endswith("." + HOTE_DEMO) and d.count(".") == HOTE_DEMO.count(".") + 1):
        return ecrire_etat(d, "refuse", message="sous-domaine démo invalide"), True
    if type_ == "client" and HOTE_DEMO and (d == HOTE_DEMO or d.endswith("." + HOTE_DEMO)):
        return ecrire_etat(d, "refuse", message="domaine réservé"), True
    proteges = domaines_proteges()
    if d in proteges or f"www.{d}" in proteges:
        return ecrire_etat(d, "refuse", message="ce domaine est déjà utilisé par un autre service du serveur"), True
    deja = d in geres()
    if not deja and len(geres()) >= MAX_SITES:
        return ecrire_etat(d, "refuse", message=f"plafond de {MAX_SITES} sites atteint"), True
    adr = ips(d)
    if IP_VPS not in adr:
        age = time.time() - source.stat().st_mtime
        if age > DELAI_ABANDON:
            return ecrire_etat(d, "abandon", message="le DNS ne pointe toujours pas vers le serveur après 7 jours", dns_ips=adr), True
        ecrire_etat(d, "dns_en_attente", message=f"le domaine pointe vers {', '.join(adr) or 'rien'} au lieu de {IP_VPS}", dns_ips=adr)
        return None, False   # garder la demande (file d'attente)
    noms = [d]
    if type_ == "client" and IP_VPS in ips("www." + d) and f"www.{d}" not in proteges:
        noms.append("www." + d)
    fichier = NGINX_DISPO / f"pk-site-{d}"
    lien = NGINX_ACTIFS / f"pk-site-{d}"
    a_cert = (LE_LIVE / d).is_dir()
    if not deja or not a_cert:
        if not SIMULATION or NGINX_DISPO.is_dir():
            fichier.write_text(conf_nginx(noms))
            if not lien.exists():
                lien.symlink_to(fichier)
        ok, err = recharger_nginx()
        if not ok:
            fichier.unlink(missing_ok=True)
            lien.unlink(missing_ok=True)
            recharger_nginx()
            return ecrire_etat(d, "erreur", message="nginx : " + err), True
    if a_cert:
        return ecrire_etat(d, "actif", https=True, noms=noms, essais_cert=0), True
    try:
        essais = lire_json(ETAT / f"{d}.json").get("essais_cert", 0)
    except (OSError, ValueError):
        essais = 0
    if essais >= MAX_ESSAIS_CERT:
        return ecrire_etat(d, "http_seulement", message="certificat HTTPS refusé plusieurs fois : vérifier le DNS puis relancer", noms=noms), True
    cmd = ["certbot", "--nginx", "--non-interactive", "--agree-tos", "--redirect", "--cert-name", d]
    for n in noms:
        cmd += ["-d", n]
    if ENV.get("CERTBOT_EMAIL"):
        cmd += ["-m", ENV["CERTBOT_EMAIL"]]
    else:
        cmd += ["--register-unsafely-without-email"]
    r = lancer(cmd, timeout=300)
    if r.returncode == 0:
        return ecrire_etat(d, "actif", https=True, noms=noms, essais_cert=0), True
    ecrire_etat(d, "http_seulement", message="certificat en échec : " + (r.stderr or r.stdout)[-300:], noms=noms, essais_cert=essais + 1)
    return None, False


def retirer(dem: dict):
    d = dem["domaine"]
    fichier, lien = NGINX_DISPO / f"pk-site-{d}", NGINX_ACTIFS / f"pk-site-{d}"
    if fichier.exists() or lien.is_symlink():
        lien.unlink(missing_ok=True)
        fichier.unlink(missing_ok=True)
        recharger_nginx()
    if (LE_LIVE / d).is_dir():
        lancer(["certbot", "delete", "--cert-name", d, "--non-interactive"])
    try:
        (ETAT / f"{d}.json").unlink()
    except FileNotFoundError:
        pass
    journal(f"{d} retiré")


def traiter(chemin: Path):
    try:
        dem = lire_json(chemin)
        d = str(dem.get("domaine", "")).lower()
        action = dem.get("action")
        if not RE_DOMAINE.match(d) or action not in ("ajouter", "retirer") or dem.get("type", "demo") not in ("demo", "client"):
            raise ValueError("demande invalide")
        dem["domaine"] = d
    except (OSError, ValueError) as ex:
        journal(f"demande ignorée {chemin.name} : {ex}")
        chemin.unlink(missing_ok=True)
        return
    if action == "retirer":
        retirer(dem)
        chemin.unlink(missing_ok=True)
        (ATT / f"{d}.json").unlink(missing_ok=True)
        return
    try:
        _, fini = ajouter(dem, chemin)
    except Exception as ex:  # ne jamais bloquer la file
        ecrire_etat(d, "erreur", message=str(ex)[:300])
        fini = True
    if fini:
        chemin.unlink(missing_ok=True)
        (ATT / f"{d}.json").unlink(missing_ok=True)
    elif chemin.parent != ATT:
        os.replace(chemin, ATT / chemin.name)   # conserve la date de dépôt (abandon après 7 jours)


def main():
    verifier_dossiers()
    with open(H / ".verrou", "a") as v:
        fcntl.flock(v, fcntl.LOCK_EX)
        fichiers = sorted(p for p in DEM.glob("*.json") if not p.is_symlink())[:50]
        if "--tout" in sys.argv:
            fichiers += sorted(p for p in ATT.glob("*.json") if not p.is_symlink())[:50]
        for p in DEM.iterdir():   # nettoyage : liens, restes de fichiers temporaires (sinon systemd reboucle)
            if p.is_symlink() or (not p.name.endswith(".json") and time.time() - p.lstat().st_mtime > 60):
                p.unlink(missing_ok=True)
        for p in fichiers:
            traiter(p)


if __name__ == "__main__":
    if not IP_VPS:
        raise SystemExit("ATELIER_IP_VPS absent de /opt/pk-atelier/.env")
    main()
