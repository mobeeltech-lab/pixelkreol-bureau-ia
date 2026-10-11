"""Réglages du standard, lus dans les variables d'environnement ABI_STANDARD_* (voir .env.exemple)."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("abi_standard")

CATEGORIES = ("vente", "location", "appel_de_fonds", "chantier", "fournisseur", "administration", "autre")
# Agent du Bureau IA qui reprend normalement chaque catégorie (la Secrétaire peut en proposer un autre, s'il existe).
AGENT_PAR_CATEGORIE = {"vente": "commercial", "location": "locatif", "appel_de_fonds": "appels-de-fonds",
                       "chantier": "suivi-programme", "fournisseur": "comptable", "administration": "juriste",
                       "autre": "secretaire"}
AGENTS_CONNUS = ("orchestrateur", "secretaire", "comptable", "appels-de-fonds", "commercial", "locatif", "juriste",
                 "suivi-programme", "archiviste")
ADRESSE_RE = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def vrai(valeur: str) -> bool:
    return str(valeur).strip().lower() in ("1", "true", "oui", "yes", "on")


def _nombre(env, nom, defaut, mini, maxi, entier=True):
    try:
        v = float(env.get(nom, "") or defaut)
    except ValueError:
        LOG.warning("%s illisible, valeur par défaut %s utilisée", nom, defaut)
        v = defaut
    v = max(mini, min(maxi, v))
    return int(v) if entier else v


def liste_adresses(texte: str) -> list[str]:
    sortie = []
    for a in re.split(r"[,;\s]+", texte or ""):
        a = a.strip().lower()
        if a and a not in sortie:
            sortie.append(a)
    return sortie


@dataclass
class Config:
    actif: bool = False
    # --- boîte mail dédiée (IMAP)
    imap_hote: str = ""
    imap_port: int = 993
    imap_ssl: bool = True
    imap_compte: str = ""
    imap_mdp: str = ""
    dossier_source: str = "INBOX"
    dossier_traite: str = "Traité"
    apres: str = "lu"                       # « lu » : marqué lu seulement ; « ranger » : copié dans le dossier traité
    expediteurs_ok: list[str] = field(default_factory=list)
    # --- envoi des synthèses (SMTP)
    smtp_hote: str = ""
    smtp_port: int = 587
    smtp_securite: str = "starttls"         # starttls | ssl | aucune
    smtp_compte: str = ""
    smtp_mdp: str = ""
    smtp_expediteur: str = ""
    # --- destinataires
    destinataires: dict[str, list[str]] = field(default_factory=dict)
    dest_defaut: list[str] = field(default_factory=list)
    dest_urgence: list[str] = field(default_factory=list)
    domaines_ok: list[str] = field(default_factory=list)
    # --- horaires d'ouverture (servent à dater les rappels et à signaler un message reçu hors horaires)
    horaires: str = "08:00-18:00"
    jours: str = "1-5"
    fuseau: str = "Europe/Paris"
    # --- transcription locale
    whisper_modele: str = "small"
    whisper_dossier: str = "/models"
    threads: int = 2
    beam: int = 1
    duree_max_s: int = 180
    taille_max_mo: int = 15
    mots_min: int = 3
    liberer_modele: bool = True
    # --- Secrétaire (orchestrateur du Bureau IA)
    orch_url: str = "http://abi_orchestrateur:4001"
    orch_cle: str = ""
    orch_agent: str = "secretaire"
    orch_delai: int = 120
    # --- fonctionnement
    intervalle_s: int = 120
    lot_max: int = 5
    max_mails_heure: int = 30
    max_alertes_heure: int = 10
    conservation_jours: int = 30
    conservation_journal_jours: int = 365
    dossier_donnees: str = "/app/data"
    dossier_tmp: str = "/tmp"

    @property
    def base(self) -> Path:
        return Path(self.dossier_donnees) / "standard.db"

    @classmethod
    def depuis_env(cls, env=None) -> "Config":
        e = os.environ if env is None else env
        g = lambda n, d="": (e.get("ABI_STANDARD_" + n, "") or d).strip()
        c = cls(
            actif=vrai(g("ACTIF", "0")),
            imap_hote=g("IMAP_HOTE"), imap_port=_nombre(e, "ABI_STANDARD_IMAP_PORT", 993, 1, 65535),
            imap_ssl=vrai(g("IMAP_SSL", "1")), imap_compte=g("IMAP_COMPTE"), imap_mdp=e.get("ABI_STANDARD_IMAP_MDP", ""),
            dossier_source=g("IMAP_DOSSIER", "INBOX"), dossier_traite=g("IMAP_DOSSIER_TRAITE", "Traité"),
            apres="ranger" if g("IMAP_APRES", "lu").lower() == "ranger" else "lu",
            expediteurs_ok=liste_adresses(g("EXPEDITEURS_OK")),
            smtp_hote=g("SMTP_HOTE"), smtp_port=_nombre(e, "ABI_STANDARD_SMTP_PORT", 587, 1, 65535),
            smtp_securite=g("SMTP_SECURITE", "starttls").lower(), smtp_compte=g("SMTP_COMPTE"),
            smtp_mdp=e.get("ABI_STANDARD_SMTP_MDP", ""), smtp_expediteur=g("SMTP_EXPEDITEUR"),
            dest_defaut=liste_adresses(g("DEST_DEFAUT")), dest_urgence=liste_adresses(g("DEST_URGENCE")),
            domaines_ok=[d.lower().lstrip("@") for d in liste_adresses(g("DOMAINES_OK"))],
            horaires=g("HORAIRES", "08:00-18:00"), jours=g("JOURS", "1-5"), fuseau=g("FUSEAU", "Europe/Paris"),
            whisper_modele=g("WHISPER_MODELE", "small"), whisper_dossier=g("WHISPER_DOSSIER", "/models"),
            threads=_nombre(e, "ABI_STANDARD_THREADS", 2, 1, 4), beam=_nombre(e, "ABI_STANDARD_BEAM", 1, 1, 5),
            duree_max_s=_nombre(e, "ABI_STANDARD_DUREE_MAX", 180, 10, 900),
            taille_max_mo=_nombre(e, "ABI_STANDARD_TAILLE_MAX_MO", 15, 1, 100),
            mots_min=_nombre(e, "ABI_STANDARD_MOTS_MIN", 3, 1, 20),
            liberer_modele=vrai(g("LIBERER_MODELE", "1")),
            orch_url=g("ORCH_URL", "http://abi_orchestrateur:4001").rstrip("/"), orch_cle=e.get("ABI_STANDARD_ORCH_CLE", ""),
            orch_agent=g("AGENT", "secretaire"), orch_delai=_nombre(e, "ABI_STANDARD_ORCH_DELAI", 120, 5, 600),
            intervalle_s=_nombre(e, "ABI_STANDARD_INTERVALLE", 120, 20, 3600),
            lot_max=_nombre(e, "ABI_STANDARD_LOT_MAX", 5, 1, 50),
            max_mails_heure=_nombre(e, "ABI_STANDARD_MAX_MAILS_HEURE", 30, 1, 500),
            max_alertes_heure=_nombre(e, "ABI_STANDARD_MAX_ALERTES_HEURE", 10, 1, 100),
            conservation_jours=_nombre(e, "ABI_STANDARD_CONSERVATION_JOURS", 30, 1, 3650),
            conservation_journal_jours=_nombre(e, "ABI_STANDARD_CONSERVATION_JOURNAL", 365, 1, 3650),
            dossier_donnees=g("DONNEES", "/app/data"), dossier_tmp=g("TMP", "/tmp"),
        )
        for cat in CATEGORIES:
            c.destinataires[cat] = liste_adresses(g("DEST_" + cat.upper()))
        return c

    def problemes(self) -> list[str]:
        """Ce qui empêche de démarrer (liste vide = prêt). Le standard refuse de tourner à moitié configuré."""
        p = []
        if not self.imap_hote or not self.imap_compte or not self.imap_mdp:
            p.append("boîte IMAP incomplète (ABI_STANDARD_IMAP_HOTE, _COMPTE, _MDP)")
        if not self.smtp_hote or not self.smtp_expediteur:
            p.append("envoi SMTP incomplet (ABI_STANDARD_SMTP_HOTE, _EXPEDITEUR)")
        if self.smtp_securite not in ("starttls", "ssl", "aucune"):
            p.append("ABI_STANDARD_SMTP_SECURITE doit valoir starttls, ssl ou aucune")
        tous = set(self.dest_defaut) | set(self.dest_urgence) | {a for l in self.destinataires.values() for a in l}
        if not self.dest_defaut:
            p.append("aucun destinataire par défaut (ABI_STANDARD_DEST_DEFAUT) : les messages sans catégorie seraient perdus")
        for a in sorted(tous):
            if not ADRESSE_RE.match(a):
                p.append(f"adresse de destinataire illisible : {a}")
        if self.domaines_ok:
            for a in sorted(tous):
                if a.rsplit("@", 1)[-1] not in self.domaines_ok:
                    p.append(f"destinataire hors des domaines autorisés : {a}")
        if not self.orch_cle:
            p.append("clé de l'orchestrateur absente (ABI_API_KEY transmise par le compose)")
        if self.imap_compte.lower() in tous:
            p.append("la boîte dédiée ne peut pas être aussi destinataire des synthèses")
        return p
