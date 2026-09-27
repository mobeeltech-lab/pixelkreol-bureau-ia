"""Hébergement autonome : l'atelier dépose des demandes que le service hôte « pk-hebergeur » applique
(configuration nginx + certificat HTTPS Let's Encrypt), puis lit l'état en retour.

Le conteneur n'a aucun droit sur nginx : il écrit seulement des fichiers JSON dans /data/hebergement/demandes,
le service hôte (root) les valide strictement avant d'agir."""
import json
import os
import re
import socket

from . import config, store

RE_DOMAINE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def normaliser(domaine: str) -> str:
    d = (domaine or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split(":")[0].rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    try:
        d = d.encode("idna").decode()
    except UnicodeError:
        pass
    if not RE_DOMAINE.match(d):
        raise ValueError(f"Nom de domaine invalide : {domaine}")
    return d


def hote_sous_domaine(slug: str) -> str:
    return f"{slug}.{config.HOTE_DEMO}"


def ips(domaine: str) -> list:
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(domaine, None, socket.AF_INET)})
    except OSError:
        return []


def dns_ok(domaine: str) -> bool:
    return bool(config.IP_VPS) and config.IP_VPS in ips(domaine)


def demander(domaine: str, action: str, site: str = "", type_: str = "demo") -> dict:
    """Dépose une demande pour le service hôte (écriture atomique)."""
    if action not in ("ajouter", "retirer"):
        raise ValueError("action inconnue")
    d = normaliser(domaine) if type_ == "client" else domaine.lower()
    if not RE_DOMAINE.match(d):
        raise ValueError(f"Nom de domaine invalide : {domaine}")
    dossier = config.HEBERG / "demandes"
    tmp = dossier / f".{d}.tmp"
    tmp.write_text(json.dumps({"domaine": d, "action": action, "site": site, "type": type_,
                               "quand": store.maintenant().isoformat(timespec="seconds")}))
    os.replace(tmp, dossier / f"{d}.json")
    store.journal("hebergement_demande", {"domaine": d, "action": action, "site": site})
    return {"domaine": d, "action": action, "demande": True}


def etat(domaine: str) -> dict:
    f = config.HEBERG / "etat" / f"{domaine}.json"
    if f.is_file() and not f.is_symlink():
        try:
            return json.loads(f.read_text())
        except ValueError:
            pass
    en_cours = any((config.HEBERG / x / f"{domaine}.json").exists() for x in ("demandes", "attente"))
    return {"domaine": domaine, "etat": "en_cours" if en_cours else "inconnu", "https": False}


def instructions_dns(domaine: str) -> str:
    ip = config.IP_VPS or "<IP du VPS>"
    return (f"Chez le registrar du domaine {domaine} (OVH, Gandi, Hostinger…), zone DNS : "
            f"enregistrement A « @ » → {ip} et enregistrement A « www » → {ip} "
            f"(supprimer les anciens A/AAAA de @ et www). Le certificat HTTPS se crée tout seul dès que le DNS a propagé "
            f"(quelques minutes à quelques heures).")
