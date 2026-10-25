"""Orchestrateur du Bureau IA PixelKréol.

API compatible OpenAI placée devant LiteLLM :
  * chaque agent du registre apparaît comme un « modèle » (pk-02-commercial…) avec sa
    fiche de poste injectée automatiquement ;
  * `pk-auto`   : choisit tout seul le bon agent (mots-clés, puis IA si doute) ;
  * `pk-equipe` : le directeur de cabinet découpe la demande, plusieurs agents
    travaillent en parallèle, puis il fait la synthèse ;
  * bascule automatique entre cerveaux si un cerveau entier est indisponible ;
  * registre relu à chaud : ajouter un agent ne demande aucun redémarrage.
"""
from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import importlib.util
import hmac
import html
import json
import logging
import os
import re
import sys
import tempfile
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

LOG = logging.getLogger("pk")
logging.basicConfig(level=os.getenv("PK_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

VERSION = "2.0.0"
CERVEAUX_VALIDES = {"pk-rapide", "pk-costaud", "pk-code", "pk-vision", "pk-long"}
FAMILLES = {"direction", "vente", "production", "gestion", "relation", "technique", "produit", "client"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def cfg(nom: str, defaut: str = "") -> str:
    return os.getenv(nom, defaut)


def cfg_nombre(nom: str, defaut: float, mini: float, maxi: float, entier: bool = False):
    """Lit un réglage numérique ; une valeur illisible ou absurde ne doit jamais empêcher le démarrage."""
    try:
        v = float(cfg(nom, str(defaut)))
    except ValueError:
        LOG.warning("%s illisible (« %s »), valeur par défaut %s utilisée", nom, cfg(nom), defaut)
        v = defaut
    v = max(mini, min(maxi, v))
    return int(v) if entier else v


LITELLM_URL = cfg("LITELLM_URL", "http://pk_litellm:4000").rstrip("/")
LITELLM_KEY = cfg("LITELLM_MASTER_KEY", "")
API_KEY = cfg("PK_API_KEY") or LITELLM_KEY
ADMIN_KEY = cfg("PK_ADMIN_KEY") or API_KEY
AGENTS_DIR = Path(cfg("PK_AGENTS_DIR", str(Path(__file__).resolve().parent.parent / "agents")))
TIMEOUT = cfg_nombre("PK_TIMEOUT", 120, 5, 900)
ROUTAGE_IA = cfg("PK_ROUTAGE_IA", "true").lower() == "true"
ANNONCE = cfg("PK_ANNONCE_AGENT", "true").lower() == "true"
MAX_EQUIPE = cfg_nombre("PK_EQUIPE_MAX", 4, 1, 8, entier=True)
DELAI_AGENT = cfg_nombre("PK_EQUIPE_DELAI", 150, 1, 900)   # secondes accordées à chaque agent en mode équipe
DATABASE_URL = cfg("DATABASE_URL", "")

# Textes propres à une installation (valeurs par défaut = celles de l'instance PixelKréol, inchangées).
# Une autre instance (ex. ABI Holding, tests) les remplace par des variables d'environnement.
MARQUE = cfg("PK_MARQUE", "PixelKréol")                       # « Bureau IA <marque> »
LIEU = cfg("PK_LIEU", "à Ajaccio")                            # « Date et heure <lieu> : … »
FUSEAU = cfg("PK_FUSEAU", "Europe/Paris")                      # fuseau horaire donné aux agents
PATRON = cfg("PK_PATRON", "Will")                             # « Demande de <patron> : … » (synthèse d'équipe)
VITRINE_PIED = cfg("PK_VITRINE_PIED", "LLM gratuits · La Réunion")
PROPRIETAIRE = cfg("PK_PROPRIETAIRE", "pixelkreol")           # champ owned_by du catalogue /v1/models
# Outil « documents » (lecture seule d'un NAS monté). DÉSACTIVÉ par défaut : sans PK_NAS_ACTIF=1, rien ne change.
NAS_ACTIF = cfg("PK_NAS_ACTIF", "0").strip().lower() in ("1", "true", "oui", "yes", "on")
DOCS = None            # instance de documents.Documents quand l'outil est actif
DOCUMENTS = None       # module documents.py (chargé seulement si l'outil est actif)
# Outils MCP (serveurs externes choisis par Will, lecture seule par défaut). DÉSACTIVÉ par défaut ; jamais pour une autre marque que PixelKréol (ABI : coupé).
MCP_ACTIF = cfg("PK_MCP_ACTIF", "0").strip().lower() in ("1", "true", "oui", "yes", "on")
MCP_MARQUE = cfg("PK_MCP_MARQUE", "PixelKréol")
if MCP_ACTIF and MARQUE != MCP_MARQUE:
    logging.getLogger("pk").error("PK_MCP_ACTIF=1 ignoré : MCP n'est autorisé que pour la marque « %s » (instance « %s » : MCP reste coupé)", MCP_MARQUE, MARQUE)
    MCP_ACTIF = False
# Serveur MCP du Bureau IA (expose des agents EN LECTURE SEULE à un client MCP). DÉSACTIVÉ par défaut ; jamais pour une autre marque que PixelKréol.
MCP_SERVEUR_ACTIF = cfg("PK_MCP_SERVEUR_ACTIF", "0").strip().lower() in ("1", "true", "oui", "yes", "on")
if MCP_SERVEUR_ACTIF and MARQUE != MCP_MARQUE:
    logging.getLogger("pk").error("PK_MCP_SERVEUR_ACTIF=1 ignoré : le serveur MCP n'est autorisé que pour la marque « %s » (instance « %s » : coupé)", MCP_MARQUE, MARQUE)
    MCP_SERVEUR_ACTIF = False
MCPS = None            # module mcp_serveur.py
MCPSRV = None          # instance mcp_serveur.Serveur
MCP_SERVEUR_PB = None  # raison pour laquelle le serveur MCP refuse de répondre (clé absente / courte / partagée), sinon None
MCPM = None            # module mcp_client.py (chargé seulement si MCP est actif)
MCPC = None            # gestionnaire MCP (mcp_client.MCP)
MCP_CLE_PB = None      # raison pour laquelle la validation par Will est refusée (clé admin absente / courte / partagée), sinon None
OUTILS_CONNUS = {"documents", "mcp"}
try:
    TZ = ZoneInfo(FUSEAU)
except (ZoneInfoNotFoundError, ValueError):
    LOG.warning("PK_FUSEAU « %s » inconnu : fuseau Europe/Paris utilisé", FUSEAU)
    TZ = ZoneInfo("Europe/Paris")


# =====================================================================
#  Outils texte
# =====================================================================
def normaliser(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte.lower())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    texte = re.sub(r"[^a-z0-9' -]+", " ", texte.replace("’", "'"))
    return re.sub(r"\s+", " ", texte)


_SECRETS = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{20,}|nvapi-[A-Za-z0-9_-]{16,}"
                      r"|(?:hf|ghp|gho|xox[bp])[_-][A-Za-z0-9_-]{16,}"
                      r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{6,})")
# Données personnelles et mots de passe qui ne doivent JAMAIS finir dans le sujet d'une demande (journal d'activité).
_PERSO = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[e-mail]"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?:[ -]?[A-Z0-9]{4}){2,7}(?:[ -]?[A-Z0-9]{1,4})?\b", re.I), "[IBAN]"),
    (re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"), "[carte]"),
    (re.compile(r"(?<!\w)(?:\+|00)?\d(?:[\s.()-]?\d){7,14}(?!\w)"), "[téléphone]"),
)
_MOT_DE_PASSE = re.compile(r"(?i)\b(mot\s+de\s+passe|mdp|password|passwd|pwd|passphrase|secret|jeton|token|api[ _-]?key|cl[ée]\s+(?:api|secr[èe]te))"
                           r"(\s*(?:est|is|=|:|->|→)?\s*)(\S+)")


def masquer_sujet(texte: str, longueur: int = 80) -> str:
    """Sujet d'une demande pour le journal d'activité : clés, mots de passe, e-mails, téléphones, IBAN et cartes retirés
    AVANT la coupe à `longueur` caractères (une clé coupée en deux ne doit pas laisser son début)."""
    t = " ".join(str(texte or "")[:2000].split())
    t = masquer(t)
    t = _MOT_DE_PASSE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", t)
    for motif, remplacement in _PERSO:
        t = motif.sub(remplacement, t)
    return t[:longueur]


def masquer(texte: str) -> str:
    """Retire d'un texte (message d'erreur d'un fournisseur, journal…) tout ce qui ressemble à une clé."""
    for secret in (LITELLM_KEY, API_KEY, ADMIN_KEY):
        if secret and len(secret) >= 4:
            texte = texte.replace(secret, "***")
    return _SECRETS.sub("***", texte)


def texte_du_message(message: dict) -> str:
    contenu = message.get("content")
    if isinstance(contenu, str):
        return contenu
    if isinstance(contenu, list):
        return " ".join(p.get("text", "") for p in contenu if isinstance(p, dict) and p.get("type") == "text")
    return ""


TEXTE_IMAGE_ANCIENNE = "[image envoyée plus tôt dans la conversation — non relue]"
TEXTE_IMAGE_ILLISIBLE = "[image non lue : les modèles de lecture d'images sont indisponibles pour le moment]"
AVERTISSEMENT_IMAGE = ("⚠️ Je n'ai pas pu lire l'image (modèles d'images indisponibles pour le moment) : "
                       "je réponds à partir du texte seulement.")


def _partie_image(p: Any) -> bool:
    return isinstance(p, dict) and p.get("type") in ("image_url", "input_image")


def message_a_image(m: Any) -> bool:
    c = m.get("content") if isinstance(m, dict) else None
    return isinstance(c, list) and any(_partie_image(p) for p in c)


def indice_dernier_utilisateur(messages: list[dict]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and messages[i].get("role") == "user":
            return i
    return -1


def contient_image(messages: list[dict]) -> bool:
    """Image « active » : présente dans le DERNIER message de l'utilisateur seulement (une image envoyée plus tôt
    ne doit pas forcer le cerveau d'images pour toutes les questions suivantes)."""
    i = indice_dernier_utilisateur(messages)
    return i >= 0 and message_a_image(messages[i])


def remplacer_images(messages: list[dict], texte: str, sauf: int = -1) -> list[dict]:
    """Remplace chaque image par `texte` (le reste du message est gardé), sauf dans le message d'indice `sauf`."""
    sortie = []
    for i, m in enumerate(messages):
        if i != sauf and message_a_image(m):
            m = {**m, "content": [({"type": "text", "text": texte} if _partie_image(p) else p) for p in m["content"]]}
        sortie.append(m)
    return sortie


def dernier_message_utilisateur(messages: list[dict]) -> str:
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return texte_du_message(m)
    return ""


def messages_valides(corps: dict) -> list[dict]:
    """Messages exploitables (objets uniquement) ; les éléments mal formés sont écartés."""
    brut = corps.get("messages")
    return [m for m in brut if isinstance(m, dict)] if isinstance(brut, list) else []


def aucun_agent() -> JSONResponse | None:
    """Réponse 503 claire si le registre n'a chargé aucun agent (fichier illisible…), sinon None."""
    REGISTRE.recharger()
    if REGISTRE.agents:
        return None
    return erreur(503, "Aucun agent disponible : le registre n'a pas pu être chargé. "
                       "Voir /health (champ « avertissements ») et les journaux du serveur.", "aucun_agent")


def erreur(statut: int, message: str, code: str = "erreur") -> JSONResponse:
    return JSONResponse(status_code=statut, content={"error": {"message": message, "type": code, "code": statut}})


# =====================================================================
#  Registre des agents (rechargé à chaud)
# =====================================================================
JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


class Registre:
    """Agents, socle commun et cerveaux, relus à chaud.

    Règle d'or : un fichier cassé ne fait JAMAIS disparaître les agents déjà chargés. On garde la
    dernière version valide de chaque fichier et on signale le problème dans `avertissements`.
    """

    FICHIERS_AGENTS = ("registre.yaml", "perso.yaml")

    def __init__(self, dossier: Path):
        self.dossier = dossier
        self._empreinte: tuple = ()
        self.agents: dict[str, dict] = {}
        self.socle = ""
        self.cerveaux: dict = {}
        self.avertissements: list[str] = []
        self._derniers_bons: dict[str, list] = {}   # nom de fichier -> dernières entrées lues correctement
        self.recharger(force=True)

    def _fichiers(self) -> list[Path]:
        return [self.dossier / n for n in ("registre.yaml", "perso.yaml", "socle.txt", "cerveaux.yaml")]

    def _calcul_empreinte(self) -> tuple:
        sortie = []
        for p in self._fichiers():
            try:
                st = p.stat()
                sortie.append((str(p), st.st_mtime_ns, st.st_size))
            except OSError:
                sortie.append((str(p), 0, 0))
        return tuple(sortie)

    @staticmethod
    def valider(a: Any, ids: set[str], nums: set[int]) -> str | None:
        """Renvoie la raison du refus (en français), ou None si l'agent est valide."""
        if not isinstance(a, dict):
            return "entrée illisible (un objet est attendu)"
        for champ in ("id", "num", "nom", "cerveau", "description", "consignes"):
            v = a.get(champ)
            if v is None or v == "" or (isinstance(v, (list, dict, str)) and not v):
                return f"champ « {champ} » manquant"
        for champ in ("id", "nom", "cerveau", "description", "consignes"):
            if not isinstance(a[champ], str) or not a[champ].strip():
                return f"champ « {champ} » doit être un texte"
        if not ID_RE.fullmatch(a["id"]):
            return f"id « {a['id']} » invalide (minuscules, chiffres, tirets)"
        if a["id"] in ("auto", "equipe"):
            return f"id « {a['id']} » réservé"
        if a["id"] in ids:
            return f"id « {a['id']} » déjà utilisé"
        n = a["num"]
        if isinstance(n, bool) or not (isinstance(n, int) or (isinstance(n, float) and n.is_integer())
                                       or (isinstance(n, str) and n.strip().isdigit())):
            return "num doit être un entier"
        num = int(n)
        if not 1 <= num <= 999:
            return "num doit être entre 1 et 999"
        if num in nums:
            return f"num {num} déjà utilisé"
        if a["cerveau"] not in CERVEAUX_VALIDES:
            return f"cerveau « {a['cerveau']} » inconnu ({', '.join(sorted(CERVEAUX_VALIDES))})"
        fam = a.get("famille")
        if fam is not None and (not isinstance(fam, str) or fam not in FAMILLES):
            return f"famille « {fam} » inconnue"
        if a.get("emoji") is not None and not isinstance(a["emoji"], str):
            return "emoji doit être un texte"
        t = a.get("temperature", 0.4)
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 <= t <= 1.5:
            return "temperature doit être un nombre entre 0 et 1.5"
        mots = a.get("mots_cles")
        if mots is not None and (not isinstance(mots, list) or not all(isinstance(m, str) for m in mots)):
            return "mots_cles doit être une liste de textes"
        outils = a.get("outils")
        if outils is not None and (not isinstance(outils, list) or not all(isinstance(o, str) and o in OUTILS_CONNUS for o in outils)):
            return f"outils doit être une liste parmi : {', '.join(sorted(OUTILS_CONNUS))}"
        if a.get("inclure_annuaire") is not None and not isinstance(a["inclure_annuaire"], bool):
            return "inclure_annuaire doit être true ou false"
        if a.get("routage_prioritaire_en_tete") is not None and not isinstance(a["routage_prioritaire_en_tete"], bool):
            return "routage_prioritaire_en_tete doit être true ou false"
        regles = a.get("routage_prioritaire")
        if regles is not None and not Registre.regles_valides(regles):
            return ("routage_prioritaire doit être une liste de règles ; chaque règle est une liste de groupes ; "
                    "chaque groupe est une liste de textes non vides")
        return None

    @staticmethod
    def regles_valides(regles: Any) -> bool:
        return (isinstance(regles, list) and all(
            isinstance(regle, list) and regle and all(
                isinstance(groupe, list) and groupe and all(isinstance(m, str) and m.strip() for m in groupe)
                for groupe in regle)
            for regle in regles))

    # ------------------------------------------------------------------
    def _lire_agents(self, nom: str, avert: list[str]) -> list | None:
        """Entrées du fichier, ou None s'il est illisible (le problème est alors ajouté à `avert`)."""
        p = self.dossier / nom
        if not p.exists():
            return []
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError, UnicodeDecodeError, ValueError) as e:
            avert.append(f"{nom} illisible : {str(e)[:200]}")
            return None
        if not isinstance(data, dict):
            avert.append(f"{nom} illisible : le fichier doit commencer par « agents: » suivi d'une liste")
            return None
        entrees = data.get("agents")
        if entrees is None:
            return []
        if not isinstance(entrees, list):
            avert.append(f"{nom} illisible : « agents » doit être une liste")
            return None
        return entrees

    def recharger(self, force: bool = False) -> None:
        empreinte = self._calcul_empreinte()
        if not force and empreinte == self._empreinte:
            return
        try:
            self._recharger(empreinte)
        except Exception as e:  # noqa: BLE001 — jamais de panne générale : on garde ce qui est chargé
            LOG.exception("Rechargement du registre impossible, version précédente conservée")
            self._empreinte = empreinte
            self.avertissements = [f"rechargement impossible : {str(e)[:200]}"]

    def _recharger(self, empreinte: tuple) -> None:
        avert: list[str] = []
        agents: dict[str, dict] = {}
        ids: set[str] = set()
        nums: set[int] = set()
        for nom in self.FICHIERS_AGENTS:
            entrees = self._lire_agents(nom, avert)
            if entrees is None:  # fichier illisible : on garde sa dernière version valide
                entrees = self._derniers_bons.get(nom, [])
                if entrees:
                    avert.append(f"{nom} : dernière version valide conservée")
                    LOG.error("%s illisible, dernière version valide conservée", nom)
            else:
                self._derniers_bons[nom] = entrees
            for a in entrees:
                try:
                    pb = self.valider(a, ids, nums)
                except Exception as e:  # noqa: BLE001
                    pb = f"entrée inexploitable ({type(e).__name__})"
                if pb:
                    avert.append(f"{nom} / {a.get('id', '?') if isinstance(a, dict) else '?'} ignoré : {pb}")
                    continue
                a = dict(a)
                a["num"] = int(a["num"])
                a.setdefault("famille", "client" if nom == "perso.yaml" else "production")
                a.setdefault("emoji", "🤖")
                a.setdefault("temperature", 0.4)
                a["mots_cles"] = a.get("mots_cles") or []
                a["source"] = nom
                a["modele"] = f"pk-{a['num']:02d}-{a['id']}"
                a["_mots"] = [normaliser(m).strip() for m in a["mots_cles"] if m.strip()]
                a["_regles"] = [[[normaliser(m).strip() for m in groupe] for groupe in regle]
                                for regle in a.get("routage_prioritaire") or []]
                agents[a["id"]] = a
                ids.add(a["id"])
                nums.add(a["num"])
        if not agents and self.agents:
            avert.append("aucun agent valide dans les fichiers : version précédente conservée")
            LOG.error("aucun agent valide, version précédente conservée")
            self._empreinte = empreinte
            self.avertissements = avert
            return
        socle_p = self.dossier / "socle.txt"
        cerv_p = self.dossier / "cerveaux.yaml"
        try:
            self.socle = socle_p.read_text(encoding="utf-8").strip() if socle_p.exists() else ""
        except (OSError, UnicodeDecodeError) as e:
            avert.append(f"socle.txt illisible : {e}")
        try:
            cerveaux = yaml.safe_load(cerv_p.read_text(encoding="utf-8")) if cerv_p.exists() else {}
            if cerveaux is None:
                cerveaux = {}
            if isinstance(cerveaux, dict):
                self.cerveaux = cerveaux
            else:
                avert.append("cerveaux.yaml illisible : un dictionnaire est attendu")
        except (yaml.YAMLError, OSError, UnicodeDecodeError, ValueError) as e:
            avert.append(f"cerveaux.yaml illisible : {str(e)[:200]}")
        if not agents:
            avert.append("aucun agent valide")
        self.agents = dict(sorted(agents.items(), key=lambda kv: kv[1]["num"]))
        self._empreinte = empreinte
        self.avertissements = avert
        for w in avert:
            LOG.warning(w)
        LOG.info("Registre chargé : %d agents", len(self.agents))

    # ------------------------------------------------------------------
    def trouver(self, modele: str) -> dict | None:
        self.recharger()
        m = (modele or "").strip()
        for a in self.agents.values():
            if m in (a["modele"], a["id"], f"pk-{a['id']}"):
                return a
        return None

    def secours(self, cerveau: str) -> list[str]:
        table = self.cerveaux.get("secours_entre_cerveaux") if isinstance(self.cerveaux, dict) else None
        suite = table.get(cerveau) if isinstance(table, dict) else None
        return [c for c in suite if isinstance(c, str)] if isinstance(suite, list) else []

    def annuaire(self, courant: dict) -> str:
        """Liste bornée « nom exact du modèle : description », tirée du registre chargé (perso.yaml compris)."""
        lignes, total = [], 0
        autres = sorted((x for x in self.agents.values() if x["id"] != courant["id"]), key=lambda x: x["num"])
        for x in autres:
            desc = " ".join(str(x["description"]).split())
            if len(desc) > ANNUAIRE_MAX_DESC:
                desc = desc[:ANNUAIRE_MAX_DESC - 1].rstrip() + "…"
            ligne = f"- {x['modele']} : {desc}"
            if total + len(ligne) > ANNUAIRE_MAX_CAR:
                lignes.append("- (liste tronquée : d'autres agents existent, ne les invente pas)")
                break
            lignes.append(ligne)
            total += len(ligne) + 1
        return ("## Annuaire des agents (nom exact du modèle : rôle)\n"
                "Recommande uniquement un nom de cette liste, recopié à l'identique. "
                "Pour plusieurs métiers à la fois : pk-equipe.\n" + "\n".join(lignes))

    def prompt_systeme(self, a: dict) -> str:
        maintenant = dt.datetime.now(TZ)
        date = f"{JOURS[maintenant.weekday()]} {maintenant.strftime('%d/%m/%Y %H:%M')}"
        parties = [self.socle,
                   f"## Ton poste : {a['emoji']} {a['nom']}\n{a['description']}\n\n{a['consignes'].strip()}",
                   f"Date et heure {LIEU} : {date}."]
        if DOCS is not None and "documents" in (a.get("outils") or []):
            parties.append(DOCUMENTS.INSTRUCTIONS_AGENT)
        if MCPC is not None and "mcp" in (a.get("outils") or []):
            parties.append(MCPM.INSTRUCTIONS_AGENT)
        if a["id"] == "orchestrateur":
            equipe = "\n".join(f"- {x['emoji']} {x['nom']} ({x['id']}) : {x['description']}"
                               for x in self.agents.values() if x["id"] != "orchestrateur")
            parties.append("## L'équipe que tu coordonnes\n" + equipe)
        if a.get("inclure_annuaire") is True:
            parties.append(self.annuaire(a))
        return "\n\n".join(p for p in parties if p)


ANNUAIRE_MAX_DESC = 110
ANNUAIRE_MAX_CAR = 6000

REGISTRE = Registre(AGENTS_DIR)


# =====================================================================
#  Routage automatique
# =====================================================================
POLITESSES = re.compile(r"(?<![a-z0-9])(?:aid(?:e|ez|ons)[ -](?:moi|nous)|(?:m|nous)'aider)(?![a-z0-9])")


def score_agent(a: dict, texte_norm: str) -> float:
    score = 0.0
    padded = f" {texte_norm} "
    for mot in a["_mots"]:
        if not mot:
            continue
        if " " in mot or "'" in mot:
            if mot in texte_norm:
                score += 2.0
        elif re.search(rf"(?<![a-z0-9]){re.escape(mot)}s?(?![a-z0-9])", padded):
            score += 1.0
    if re.search(rf"(?<![a-z0-9]){re.escape(normaliser(a['nom']).strip())}(?![a-z0-9])", padded):
        score += 1.5  # le nom du poste est cité tel quel (« juriste », « chef de projet »…)
    return score


def phrase_presente(mot: str, texte_norm: str) -> bool:
    """Même logique de reconnaissance que les mots-clés : expression = sous-chaîne, mot seul = mot entier (pluriel toléré)."""
    if not mot:
        return False
    if " " in mot or "'" in mot:
        return mot in texte_norm
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(mot)}s?(?![a-z0-9])", f" {texte_norm} "))


def regle_prioritaire(texte: str) -> dict | None:
    """Agent dont une règle de routage prioritaire s'applique (champ facultatif `routage_prioritaire` du registre).

    Une règle = liste de groupes ; elle s'applique quand CHAQUE groupe contient au moins une de ses expressions
    dans le message (ex. un mot de « bail » ET un verbe de relecture). Elle l'emporte sur le score des mots-clés.
    Si plusieurs agents ont une règle qui s'applique, le premier (numéro le plus bas) est choisi,
    sauf les agents marqués `routage_prioritaire_en_tete: true` (formule explicite, ex. « améliore ma demande ») qui passent avant tous les autres."""
    tn = POLITESSES.sub(" ", normaliser(texte))
    ordre = sorted(REGISTRE.agents.values(), key=lambda x: (x.get("routage_prioritaire_en_tete") is not True, x["num"]))
    for a in ordre:
        for regle in a.get("_regles") or []:
            if all(any(phrase_presente(m, tn) for m in groupe) for groupe in regle):
                return a
    return None


def classement_mots_cles(texte: str) -> list[tuple[float, dict]]:
    tn = POLITESSES.sub(" ", normaliser(texte))
    notes = [(score_agent(a, tn), a) for a in REGISTRE.agents.values()]
    return sorted(notes, key=lambda x: (-x[0], x[1]["num"]))


def agent_cite_par_l_ia(reponse: str) -> dict | None:
    """Agent désigné par la réponse de l'IA de routage, ou None si elle est floue.

    1) la réponse entière est un identifiant (avec ou sans « pk-NN- », ponctuation tolérée) ;
    2) sinon, un seul agent distinct est cité dans la phrase.
    Si plusieurs agents sont cités, on ne devine pas : les mots-clés prennent le relais."""
    nette = reponse.strip().strip("`'\"*_.,;:!() \n\t").lower()
    if nette:
        trouve = REGISTRE.trouver(nette)
        if trouve:
            return trouve
    cites = {mot for mot in re.findall(r"[a-z0-9-]+", reponse.lower()) if mot in REGISTRE.agents}
    return REGISTRE.agents[cites.pop()] if len(cites) == 1 else None


async def routage_ia(client: httpx.AsyncClient, texte: str) -> dict | None:
    liste = "\n".join(f"{a['id']}: {a['description']}" for a in REGISTRE.agents.values())
    corps = {
        "model": "pk-rapide",
        "temperature": 0,
        "max_tokens": 400,  # marge pour les modèles « à raisonnement »
        "messages": [
            {"role": "system", "content": "Tu es un aiguilleur. Réponds UNIQUEMENT par l'identifiant de l'agent "
                                          "le plus adapté, sans autre mot.\nAgents :\n" + liste},
            {"role": "user", "content": texte[:2000]},
        ],
    }
    try:
        r = await client.post(f"{LITELLM_URL}/v1/chat/completions", json=corps,
                              headers=entetes_litellm(), timeout=12)
        if r.status_code != 200:
            LOG.warning("routage IA : LiteLLM a répondu %s (mots-clés utilisés à la place)", r.status_code)
            return None
        reponse = r.json()["choices"][0]["message"]["content"] or ""
        agent = agent_cite_par_l_ia(reponse)
        if agent is None:
            LOG.warning("routage IA : réponse inexploitable (« %s »), mots-clés utilisés à la place",
                        masquer(reponse.strip())[:80])
        return agent
    except Exception as e:  # noqa: BLE001 — le routage IA est un bonus, jamais bloquant
        LOG.warning("routage IA indisponible (%s : %s), mots-clés utilisés à la place",
                    type(e).__name__, masquer(str(e))[:120])
    return None


async def choisir_agent(client: httpx.AsyncClient, messages: list[dict]) -> tuple[dict, str]:
    messages = [m for m in messages if isinstance(m, dict)]
    texte = dernier_message_utilisateur(messages)
    prioritaire = regle_prioritaire(texte)
    if prioritaire:
        return prioritaire, "règle"
    notes = classement_mots_cles(texte)
    meilleur, a1 = notes[0] if notes else (0.0, None)
    second = notes[1][0] if len(notes) > 1 else 0.0
    if a1 and meilleur >= 2 and meilleur >= second + 1:
        return a1, "mots-clés"
    if ROUTAGE_IA and texte.strip():
        a = await routage_ia(client, texte)
        if a:
            return a, "ia"
    if a1 and meilleur > 0:
        return a1, "mots-clés"
    if contient_image(messages) and "photo-video" in REGISTRE.agents:
        return REGISTRE.agents["photo-video"], "image"
    defaut = REGISTRE.agents.get("orchestrateur") or next(iter(REGISTRE.agents.values()), None)
    if defaut is None:
        raise LookupError("aucun agent chargé")
    return defaut, "défaut"


# =====================================================================
#  Appels LiteLLM avec bascule entre cerveaux
# =====================================================================
def entetes_litellm() -> dict:
    return {"Authorization": f"Bearer {LITELLM_KEY}", "Content-Type": "application/json"}


# Options qu'un client ne doit jamais pouvoir imposer à LiteLLM (autre clé, autre serveur, autres secours…).
OPTIONS_INTERDITES = {"api_base", "api_key", "base_url", "api_version", "custom_llm_provider", "user_config",
                      "fallbacks", "disable_fallbacks", "mock_response", "model_list", "extra_headers", "headers",
                      "metadata", "organization", "deployment_id", "num_retries", "context_window_fallback_dict",
                      "caching", "cache", "proxy_server_request", "timeout", "request_timeout"}
PREFIXES_INTERDITS = ("aws_", "vertex_", "azure_", "litellm_", "mock_", "api_", "ssl_", "oci_", "watsonx_")


def option_autorisee(nom: str) -> bool:
    return nom not in OPTIONS_INTERDITES and not str(nom).startswith(PREFIXES_INTERDITS)


CHAMPS_MESSAGE = ("role", "content", "name", "tool_calls", "tool_call_id")


def nettoyer_message(m: dict) -> dict:
    """Ne garde que les champs standard d'un message : l'historique d'Open WebUI peut contenir des champs propres
    à un fournisseur (reasoning_details, reasoning_content…) que Groq, Mistral, Cerebras ou DeepSeek refusent."""
    return {k: v for k, v in m.items() if k in CHAMPS_MESSAGE}


MAX_MESSAGE = int(cfg_nombre("PK_MAX_MESSAGE", 24000, 2000, 400000))   # caracteres par message envoye aux cerveaux
MAX_TOTAL = int(cfg_nombre("PK_MAX_TOTAL", 90000, 8000, 2000000))       # caracteres pour toute la conversation


def tronquer_milieu(t, maxi: int):
    """Coupe le MILIEU d'un texte trop long (pages web lues par un outil) : le debut (consignes) et la fin (question) restent."""
    if not isinstance(t, str) or len(t) <= maxi:
        return t
    tete = int(maxi * 0.6)
    return (t[:tete] + f"\n\n[… {len(t) - maxi} caractères retirés ici pour tenir dans la mémoire des cerveaux …]\n\n"
            + t[-(maxi - tete):])


def _taille(m: dict) -> int:
    c = m.get("content")
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        return sum(len(x.get("text", "")) for x in c if isinstance(x, dict) and isinstance(x.get("text"), str))
    return 0


def borner(messages: list[dict]) -> list[dict]:
    """Chaque message est borne a MAX_MESSAGE caracteres, puis les echanges les plus anciens sont retires tant que la
    conversation depasse MAX_TOTAL (les 2 derniers messages sont toujours gardes)."""
    ms = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            m = {**m, "content": tronquer_milieu(c, MAX_MESSAGE)}
        elif isinstance(c, list):
            m = {**m, "content": [({**x, "text": tronquer_milieu(x["text"], MAX_MESSAGE)}
                                   if isinstance(x, dict) and isinstance(x.get("text"), str) else x) for x in c]}
        ms.append(m)
    while sum(_taille(m) for m in ms) > MAX_TOTAL and len(ms) > 2:
        del ms[0]
        while len(ms) > 2 and ms[0].get("role") == "tool":   # pas de resultat d'outil orphelin
            del ms[0]
    return ms


class ChaineCerveaux(list):
    """Liste de cerveaux à essayer. `repli` (facultatif) : chaîne TEXTE à utiliser, images retirées, si le cerveau d'images échoue."""

    def __init__(self, cerveaux, agent_id: str = "", repli: list[str] | None = None):
        super().__init__(cerveaux)
        self.agent_id, self.repli = agent_id, repli


def preparer_corps(corps: dict, agent: dict) -> tuple[dict, list[str]]:
    messages = [m for m in corps.get("messages", []) if isinstance(m, dict)]
    systeme_client = tronquer_milieu("\n\n".join(texte_du_message(m) for m in messages if m.get("role") == "system").strip(), MAX_MESSAGE)
    systeme = REGISTRE.prompt_systeme(agent)
    if systeme_client:
        systeme += "\n\n## Consignes supplémentaires de l'utilisateur\n" + systeme_client
    sans_systeme = [nettoyer_message(m) for m in messages if m.get("role") != "system"]
    image_active = contient_image(sans_systeme)
    # ordre : nettoyer -> remplacer les images anciennes -> borner
    sans_systeme = remplacer_images(sans_systeme, TEXTE_IMAGE_ANCIENNE, sauf=indice_dernier_utilisateur(sans_systeme) if image_active else -1)
    nouveaux = [{"role": "system", "content": systeme}] + borner(sans_systeme)
    sortie = {k: v for k, v in corps.items() if k not in ("messages", "model") and option_autorisee(k)}
    sortie["messages"] = nouveaux
    sortie.setdefault("temperature", agent.get("temperature", 0.4))
    cerveau = agent["cerveau"]
    normale = [cerveau] + [c for c in REGISTRE.secours(cerveau) if c != cerveau]
    if not image_active:
        return sortie, ChaineCerveaux(normale, agent["id"])
    # image dans le dernier message : cerveau d'images d'abord ; s'il est épuisé, repli TEXTE (images retirées, avertissement)
    repli = [c for c in normale if c != "pk-vision"]
    return sortie, ChaineCerveaux(["pk-vision"], agent["id"], repli)


class EchecCerveaux(Exception):
    def __init__(self, statut: int, detail: str, essayes: list[str] | None = None):
        super().__init__(detail)
        self.statut, self.detail, self.essayes = statut, detail, essayes or []

    def message(self, nom_agent: str) -> str:
        essais = f" (essayés : {', '.join(self.essayes)})" if self.essayes else ""
        return f"Aucun cerveau disponible pour {nom_agent}{essais} : {self.detail}"


def statut_rebascule(statut: int) -> bool:
    return statut in (404, 408, 429) or statut >= 500


def reponse_vide(rep: Any) -> bool:
    """Vrai si LiteLLM a répondu 200 mais sans aucun contenu exploitable (ni texte, ni appel d'outil)."""
    try:
        msg = rep["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return True
    if not isinstance(msg, dict):
        return True
    contenu = msg.get("content")
    return not (isinstance(contenu, str) and contenu.strip()) and not msg.get("tool_calls") and not msg.get("function_call")


def texte_de_la_reponse(rep: dict) -> str:
    try:
        contenu = rep["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    return contenu if isinstance(contenu, str) else ""


def erreur_du_bloc(bloc: bytes) -> tuple[int, str] | None:
    """Si le premier bloc d'un flux est une erreur envoyée par LiteLLM, renvoie (statut, message)."""
    for ligne in bloc.decode("utf-8", "replace").splitlines():
        if not ligne.startswith("data:"):
            continue
        brut = ligne[5:].strip()
        if not brut or brut == "[DONE]":
            return None
        try:
            obj = json.loads(brut)
        except ValueError:
            return None
        err = obj.get("error") if isinstance(obj, dict) else None
        if not err:
            return None
        code = err.get("code") if isinstance(err, dict) else None
        statut = int(code) if str(code).isdigit() and int(code) >= 400 else 502
        message = err.get("message") if isinstance(err, dict) else str(err)
        return statut, str(message)[:300]
    return None


async def _appeler(client: httpx.AsyncClient, corps: dict, chaine: list[str]) -> tuple[dict, str]:
    dernier = (502, "aucun cerveau essayé")
    essayes: list[str] = []
    for cerveau in chaine:
        essayes.append(cerveau)
        t0 = time.monotonic()
        try:
            r = await client.post(f"{LITELLM_URL}/v1/chat/completions", json={**corps, "model": cerveau, "stream": False},
                                  headers=entetes_litellm(), timeout=TIMEOUT)
        except httpx.HTTPError as e:
            delai = isinstance(e, httpx.TimeoutException)
            dernier = (504 if delai else 503, f"{cerveau} {'délai dépassé' if delai else 'injoignable'} ({type(e).__name__})")
            LOG.warning("bascule : %s", dernier[1])
            JOURNAL.maillon(cerveau, dernier[0], "", "trafic", int((time.monotonic() - t0) * 1000))
            continue
        if r.status_code == 200:
            try:
                rep = r.json()
            except ValueError:
                dernier = (502, f"{cerveau} : réponse illisible (pas du JSON)")
                LOG.warning("bascule : %s", dernier[1])
                JOURNAL.maillon(cerveau, 502, "", "trafic", int((time.monotonic() - t0) * 1000))
                continue
            if reponse_vide(rep):
                dernier = (502, f"{cerveau} : réponse vide")
                LOG.warning("bascule : %s", dernier[1])
                JOURNAL.maillon(cerveau, 502, "", "trafic", int((time.monotonic() - t0) * 1000))
                continue
            modele = fournisseur_reel(rep.get("model") if isinstance(rep, dict) else None, r.headers, client)
            noter_fournisseur(modele)
            JOURNAL.maillon(cerveau, 200, modele or "", "trafic", int((time.monotonic() - t0) * 1000))
            return rep, cerveau
        dernier = (r.status_code, masquer(f"{cerveau} : {r.text[:300]}"))
        JOURNAL.maillon(cerveau, r.status_code, "", "trafic", int((time.monotonic() - t0) * 1000))
        if not statut_rebascule(r.status_code):
            break
        LOG.warning("bascule : %s a répondu %s", cerveau, r.status_code)
    raise EchecCerveaux(*dernier, essayes=essayes)


def modele_du_bloc(bloc: bytes) -> str:
    """Champ « model » du premier bloc d'un flux SSE (le modèle réellement servi par LiteLLM), sinon chaîne vide."""
    m = re.search(rb'"model"\s*:\s*"([^"\\]{1,100})"', bloc[:4096])
    return m.group(1).decode("utf-8", "replace") if m else ""


async def _ouvrir_flux(client: httpx.AsyncClient, corps: dict, chaine: list[str]):
    """Ouvre le flux du premier cerveau qui démarre vraiment. Renvoie (réponse, cerveau, 1er bloc, suite).

    On lit le premier bloc AVANT de s'engager : un flux vide ou qui commence par une erreur permet encore
    de basculer sur le cerveau suivant. Après le premier octet transmis, on ne rebascule plus
    (sinon la réponse serait facturée deux fois)."""
    dernier = (502, "aucun cerveau essayé")
    essayes: list[str] = []
    for cerveau in chaine:
        essayes.append(cerveau)
        t0 = time.monotonic()
        req = client.build_request("POST", f"{LITELLM_URL}/v1/chat/completions",
                                   json={**corps, "model": cerveau, "stream": True},
                                   headers=entetes_litellm(), timeout=httpx.Timeout(TIMEOUT, connect=10))
        try:
            r = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            delai = isinstance(e, httpx.TimeoutException)
            dernier = (504 if delai else 503, f"{cerveau} {'délai dépassé' if delai else 'injoignable'} ({type(e).__name__})")
            LOG.warning("bascule (flux) : %s", dernier[1])
            JOURNAL.maillon(cerveau, dernier[0], "", "trafic", int((time.monotonic() - t0) * 1000))
            continue
        if r.status_code != 200:
            texte = (await r.aread()).decode("utf-8", "replace")[:300]
            await r.aclose()
            dernier = (r.status_code, masquer(f"{cerveau} : {texte}"))
            JOURNAL.maillon(cerveau, r.status_code, "", "trafic", int((time.monotonic() - t0) * 1000))
            if not statut_rebascule(r.status_code):
                break
            LOG.warning("bascule (flux) : %s a répondu %s", cerveau, r.status_code)
            continue
        suite = r.aiter_raw()
        try:
            premier = await suite.__anext__()
        except StopAsyncIteration:
            premier = b""
        except httpx.HTTPError as e:
            await r.aclose()
            dernier = (503, f"{cerveau} : flux coupé avant le premier octet ({type(e).__name__})")
            LOG.warning("bascule (flux) : %s", dernier[1])
            JOURNAL.maillon(cerveau, 503, "", "trafic", int((time.monotonic() - t0) * 1000))
            continue
        if not premier.strip():
            await r.aclose()
            dernier = (502, f"{cerveau} : flux vide")
            LOG.warning("bascule (flux) : %s", dernier[1])
            JOURNAL.maillon(cerveau, 502, "", "trafic", int((time.monotonic() - t0) * 1000))
            continue
        err = erreur_du_bloc(premier)
        if err:
            await r.aclose()
            dernier = (err[0], masquer(f"{cerveau} : {err[1]}"))
            JOURNAL.maillon(cerveau, err[0], "", "trafic", int((time.monotonic() - t0) * 1000))
            if not statut_rebascule(err[0]):
                break
            LOG.warning("bascule (flux) : %s a signalé l'erreur %s", cerveau, err[0])
            continue
        modele = fournisseur_reel(modele_du_bloc(premier), r.headers, client)
        noter_fournisseur(modele)
        JOURNAL.maillon(cerveau, 200, modele, "trafic", int((time.monotonic() - t0) * 1000))
        return r, cerveau, premier, suite
    raise EchecCerveaux(*dernier, essayes=essayes)


def corps_sans_images(corps: dict) -> dict:
    return {**corps, "messages": remplacer_images(corps.get("messages", []), TEXTE_IMAGE_ILLISIBLE)}


def repli_texte_possible(chaine: Any, e: "EchecCerveaux") -> bool:
    """Le cerveau d'images a échoué avec une erreur qui justifie une bascule (429, 404, 408, 5xx, réponse vide).
    Une erreur 400 / 401 / 403 (requête refusée, clé invalide) ne déclenche PAS de nouvel envoi ailleurs."""
    return bool(getattr(chaine, "repli", None)) and statut_rebascule(e.statut)


def journaliser_repli_texte(chaine: Any, e: "EchecCerveaux", cerveau: str) -> None:
    LOG.warning("bascule vision -> texte : agent=%s cerveau_origine=%s cerveau_utilise=%s statut=%s",
                getattr(chaine, "agent_id", "?"), "pk-vision", cerveau, e.statut)


def avertir_image(rep: dict) -> dict:
    """Ajoute en tête du texte final la ligne d'avertissement (jamais sur une réponse qui n'est qu'un appel d'outil)."""
    try:
        msg = rep["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return rep
    if isinstance(msg, dict) and not msg.get("tool_calls") and not msg.get("function_call"):
        msg["content"] = AVERTISSEMENT_IMAGE + "\n\n" + (msg.get("content") if isinstance(msg.get("content"), str) else "")
    return rep


async def appeler(client: httpx.AsyncClient, corps: dict, chaine: list[str]) -> tuple[dict, str]:
    try:
        return await _appeler(client, corps, chaine)
    except EchecCerveaux as e:
        if not repli_texte_possible(chaine, e):
            raise
        try:
            rep, cerveau = await _appeler(client, corps_sans_images(corps), list(chaine.repli))
        except EchecCerveaux as e2:
            e2.essayes = e.essayes + e2.essayes
            raise
        journaliser_repli_texte(chaine, e, cerveau)
        return avertir_image(rep), cerveau


async def ouvrir_flux(client: httpx.AsyncClient, corps: dict, chaine: list[str]):
    """Comme _ouvrir_flux, avec le repli texte du cerveau d'images. Renvoie (réponse, cerveau, 1er bloc, suite, avertissement)."""
    try:
        return (*await _ouvrir_flux(client, corps, chaine), "")
    except EchecCerveaux as e:
        if not repli_texte_possible(chaine, e):
            raise
        try:
            r, cerveau, premier, suite = await _ouvrir_flux(client, corps_sans_images(corps), list(chaine.repli))
        except EchecCerveaux as e2:
            e2.essayes = e.essayes + e2.essayes
            raise
        journaliser_repli_texte(chaine, e, cerveau)
        return r, cerveau, premier, suite, AVERTISSEMENT_IMAGE + "\n\n"


def morceau_sse(modele: str, contenu: str | None = None, fin: str | None = None, cid: str | None = None) -> bytes:
    delta: dict[str, Any] = {}
    if contenu is not None:
        delta = {"role": "assistant", "content": contenu}
    obj = {"id": cid or f"chatcmpl-pk-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
           "created": int(time.time()), "model": modele,
           "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


def completion_simple(modele: str, contenu: str, usage: dict | None = None) -> dict:
    return {"id": f"chatcmpl-pk-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()),
            "model": modele,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": contenu}, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def cumuler_usage(total: dict | None, rep: Any) -> None:
    """Additionne le champ `usage` d'une réponse LiteLLM dans `total` (sans jamais lever d'erreur)."""
    if total is None:
        return
    try:
        u = rep.get("usage") or {}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            total[k] = total.get(k, 0) + int(u.get(k) or 0)
    except (AttributeError, TypeError, ValueError) as e:
        LOG.debug("usage illisible, ignoré : %s", e)


async def flux_texte(modele: str, texte: str) -> AsyncIterator[bytes]:
    cid = f"chatcmpl-pk-{uuid.uuid4().hex[:12]}"
    for i in range(0, len(texte), 400):
        yield morceau_sse(modele, texte[i:i + 400], cid=cid)
    yield morceau_sse(modele, fin="stop", cid=cid)
    yield b"data: [DONE]\n\n"


# =====================================================================
#  Journal (Postgres si disponible, sinon mémoire)
# =====================================================================
# Journal d'activité (qui demande quoi à quel agent, quel maillon de la chaîne a répondu).
# Le « sujet » (80 premiers caractères de la demande, secrets et données personnelles retirés) n'est enregistré que pour
# l'instance PixelKréol ET si PK_JOURNAL_SUJET=1 : toute autre marque (ABI Holding…) le garde ÉTEINT, quel que soit le réglage.
MARQUE_SUJET = "PixelKréol"
SUJET_ACTIF = cfg("PK_JOURNAL_SUJET", "1").strip().lower() in ("1", "true", "oui", "yes", "on") and MARQUE == MARQUE_SUJET
if cfg("PK_JOURNAL_SUJET", "").strip().lower() in ("1", "true", "oui", "yes", "on") and MARQUE != MARQUE_SUJET:
    logging.getLogger("pk").error("PK_JOURNAL_SUJET=1 ignoré : le sujet des demandes n'est enregistré que pour « %s » (instance « %s » : aucun texte de demande conservé)",
                                  MARQUE_SUJET, MARQUE)
JOURNAL_JOURS = 90                  # purge automatique au-delà
ATTENTE_ECRITURE = 5.0              # secondes accordées à Postgres pour une écriture (en arrière-plan, jamais sur la réponse)
ECRITURES_MAX = 200                 # écritures simultanées en attente ; au-delà, on abandonne (Postgres bloqué) au lieu d'empiler
CTX_DEMANDE: contextvars.ContextVar = contextvars.ContextVar("pk_demande", default=None)


def ouvrir_demande(demandeur: str, corps: dict | None = None, agent: str = "", mode: str = "") -> dict:
    """Prépare le contexte de la demande en cours (lu par le journal) et l'inscrit parmi les demandes « en cours »."""
    sujet = masquer_sujet(dernier_message_utilisateur(corps.get("messages") or [])) if (SUJET_ACTIF and isinstance(corps, dict)) else ""
    ctx = {"id": uuid.uuid4().hex[:10], "demandeur": masquer(demandeur or "api")[:120], "sujet": sujet, "fournisseur": "",
           "agent": agent, "mode": mode, "debut": time.time()}
    CTX_DEMANDE.set(ctx)
    JOURNAL.commencer(ctx)
    return ctx


def noter_fournisseur(modele: Any) -> None:
    """Retient le modèle réellement utilisé par LiteLLM (champ « model » de la réponse) pour la demande en cours."""
    ctx = CTX_DEMANDE.get()
    if ctx is not None and isinstance(modele, str) and modele:
        ctx["fournisseur"] = masquer(modele)[:80]


# Modèle réellement servi : LiteLLM renvoie parfois le nom du CERVEAU (« pk-code ») dans le champ « model » (Mistral, Groq…) au lieu du
# modèle. Dans ce cas on lit les en-têtes de la réponse (déjà reçus, aucune attente) : x-litellm-model-id est traduit par /model/info,
# gardé en cache et rafraîchi EN ARRIÈRE-PLAN (au plus toutes les 10 min) ; à défaut, x-litellm-model-api-base donne au moins le fournisseur.
MODELES_LITELLM: dict[str, Any] = {"par_id": {}, "t": 0.0, "essai": 0.0, "tache": None}
RAFRAICHIR_MODELES_S = 600


def _etat_cache_modeles() -> tuple[float, float]:
    return MODELES_LITELLM["t"], MODELES_LITELLM["essai"]


async def rafraichir_modeles(client: httpx.AsyncClient) -> None:
    """Relit /model/info de LiteLLM : identifiant de déploiement -> (maillon, modèle). Ne lève jamais ; garde l'ancien cache en cas d'échec."""
    MODELES_LITELLM["essai"] = time.monotonic()
    try:
        r = await client.get(f"{LITELLM_URL}/model/info", headers=entetes_litellm(), timeout=10)
        if r.status_code != 200:
            LOG.warning("LiteLLM /model/info : erreur %s (fournisseur déduit des en-têtes seulement)", r.status_code)
            return
        table = {}
        for m in r.json().get("data") or []:
            ident = ((m.get("model_info") or {}).get("id")) if isinstance(m, dict) else None
            modele = ((m.get("litellm_params") or {}).get("model")) if isinstance(m, dict) else None
            if isinstance(ident, str) and isinstance(modele, str) and modele:
                table[ident] = {"maillon": str(m.get("model_name") or ""), "modele": modele}
        if table:
            MODELES_LITELLM["par_id"], MODELES_LITELLM["t"] = table, time.monotonic()
    except Exception as e:  # noqa: BLE001 — en arrière-plan : jamais d'erreur vers l'utilisateur
        LOG.warning("LiteLLM /model/info illisible (%s)", masquer(str(e) or type(e).__name__)[:160])


def planifier_modeles(client: httpx.AsyncClient) -> None:
    """Programme (sans attendre) la relecture de /model/info : jamais plus d'une à la fois, une par 10 min (1 min tant que le cache est vide)."""
    tache = MODELES_LITELLM["tache"]
    if tache is not None and not tache.done():
        return
    t, essai = _etat_cache_modeles()
    if time.monotonic() - essai < (RAFRAICHIR_MODELES_S if t else 60) and essai:
        return
    try:
        MODELES_LITELLM["tache"] = asyncio.get_running_loop().create_task(rafraichir_modeles(client))
    except RuntimeError:       # pas de boucle d'événements
        pass


def fournisseur_reel(modele: Any, entetes: Any, client: httpx.AsyncClient | None = None) -> str:
    """Nom du modèle réellement utilisé. Rend le champ « model » de la réponse s'il n'est pas un nom de cerveau (« pk-… »)."""
    m = modele if isinstance(modele, str) else ""
    if m and not m.startswith("pk-"):
        return m
    try:
        ident = (entetes.get("x-litellm-model-id") or "").strip() if entetes is not None else ""
        if ident:
            connu = MODELES_LITELLM["par_id"].get(ident)
            if connu:
                return connu["modele"]
            if client is not None:
                planifier_modeles(client)
        base = (entetes.get("x-litellm-model-api-base") or "").strip() if entetes is not None else ""
        hote = (urlsplit(base).hostname or "") if base else ""
        if hote:
            return hote
    except (AttributeError, ValueError):
        pass
    return m


def statut_maillon(statut: int) -> str:
    if statut == 200:
        return "ok"
    if statut in (408, 504):
        return "délai"
    if statut in (402, 404, 429):
        return str(statut)
    return "erreur" if statut >= 500 or statut < 400 else str(statut)


class Journal:
    def __init__(self) -> None:
        self.pool = None
        self.compteurs: dict[str, dict[str, int]] = {}
        self.echecs_ecriture = 0
        self.en_cours: dict[str, dict] = {}
        self.attente: set = set()
        self.abandons = 0
        self.derniere_purge = 0.0

    async def demarrer(self) -> None:
        if not DATABASE_URL:
            return
        try:
            import asyncpg  # type: ignore
            self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3, timeout=10)
            async with self.pool.acquire() as c:
                await c.execute("""CREATE TABLE IF NOT EXISTS pk_journal (
                    id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
                    agent TEXT, mode TEXT, cerveau TEXT, duree_ms INTEGER, ok BOOLEAN, detail TEXT)""")
                for col in ("demandeur", "sujet", "fournisseur"):
                    await c.execute(f"ALTER TABLE pk_journal ADD COLUMN IF NOT EXISTS {col} TEXT")
                await c.execute("CREATE INDEX IF NOT EXISTS pk_journal_ts ON pk_journal(ts)")
                await c.execute("""CREATE TABLE IF NOT EXISTS pk_maillons (
                    id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
                    maillon TEXT, fournisseur TEXT, statut INTEGER, resultat TEXT, source TEXT, duree_ms INTEGER)""")
                await c.execute("CREATE INDEX IF NOT EXISTS pk_maillons_ts ON pk_maillons(ts)")
            LOG.info("Journal Postgres actif")
            self._lancer(self._purger())
        except Exception as e:  # noqa: BLE001
            LOG.warning("Journal Postgres indisponible (%s) — journal en mémoire seulement", masquer(str(e))[:200])
            self.pool = None

    # --- demandes en cours (mémoire) -----------------------------------------------------------
    def commencer(self, ctx: dict) -> None:
        if len(self.en_cours) > 500:                       # filet : jamais de fuite mémoire
            self.en_cours.clear()
        self.en_cours[ctx["id"]] = ctx

    def demandes_en_cours(self) -> list[dict]:
        limite = time.time() - 900                           # une demande « en cours » depuis plus de 15 min est oubliée
        for k in [k for k, v in self.en_cours.items() if v["debut"] < limite]:
            self.en_cours.pop(k, None)
        return [{"debut": v["debut"], "agent": v["agent"], "mode": v["mode"], "demandeur": v["demandeur"], "sujet": v["sujet"]}
                for v in sorted(self.en_cours.values(), key=lambda v: v["debut"])]

    # --- écriture : toujours en arrière-plan ---------------------------------------------------
    def _lancer(self, coro) -> None:
        if len(self.attente) >= ECRITURES_MAX:
            coro.close()
            self.abandons += 1
            return
        t = asyncio.get_running_loop().create_task(coro)
        self.attente.add(t)
        t.add_done_callback(self.attente.discard)

    async def _executer(self, sql: str, *args) -> None:
        try:
            async def ecrire_en_base() -> None:
                async with self.pool.acquire() as con:
                    await con.execute(sql, *args)
            await asyncio.wait_for(ecrire_en_base(), ATTENTE_ECRITURE)
            self.echecs_ecriture = 0
        except Exception as e:  # noqa: BLE001 — le journal ne doit jamais bloquer une réponse
            self.echecs_ecriture += 1
            if self.echecs_ecriture == 1 or self.echecs_ecriture % 100 == 0:   # pas une ligne par requête
                LOG.warning("journal Postgres : écriture impossible (%s) — %d échec(s) de suite",
                            masquer(str(e) or type(e).__name__)[:200], self.echecs_ecriture)

    async def ecrire(self, agent: str, mode: str, cerveau: str, debut: float, ok: bool, detail: str = "") -> None:
        """Compte la demande (mémoire) puis programme l'écriture Postgres EN ARRIÈRE-PLAN : cet appel rend la main aussitôt."""
        c = self.compteurs.setdefault(agent, {"ok": 0, "echec": 0})
        c["ok" if ok else "echec"] += 1
        ctx = CTX_DEMANDE.get() or {}
        if ctx.get("id"):
            self.en_cours.pop(ctx["id"], None)
        if self.pool is None:
            return
        self._lancer(self._executer(
            "INSERT INTO pk_journal(agent,mode,cerveau,duree_ms,ok,detail,demandeur,sujet,fournisseur) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            agent, mode, cerveau, int((time.monotonic() - debut) * 1000), ok, masquer(detail)[:500],
            ctx.get("demandeur", ""), ctx.get("sujet", "") if SUJET_ACTIF else "", ctx.get("fournisseur", "")))

    def maillon(self, maillon: str, statut: int, fournisseur: str = "", source: str = "trafic", duree_ms: int = 0) -> None:
        """Résultat réel d'un appel à un cerveau (ou d'un test lancé à la main). Ne bloque jamais, ne lève jamais."""
        if self.pool is None:
            return
        try:
            self._lancer(self._executer(
                "INSERT INTO pk_maillons(maillon,fournisseur,statut,resultat,source,duree_ms) VALUES($1,$2,$3,$4,$5,$6)",
                str(maillon)[:80], masquer(str(fournisseur))[:80], int(statut), statut_maillon(int(statut)), source, int(duree_ms)))
        except RuntimeError:       # pas de boucle d'événements (appel hors serveur)
            pass

    async def _purger(self) -> None:
        for table in ("pk_journal", "pk_maillons"):
            await self._executer(f"DELETE FROM {table} WHERE ts < now() - make_interval(days => {int(JOURNAL_JOURS)})")
        self.derniere_purge = time.time()

    def purge_due(self) -> None:
        if self.pool is not None and time.time() - self.derniere_purge > 6 * 3600:
            self.derniere_purge = time.time()
            self._lancer(self._purger())

    async def arreter(self) -> None:
        if self.attente:
            await asyncio.wait(list(self.attente), timeout=3)
        if self.pool is not None:
            await self.pool.close()


JOURNAL = Journal()
ETAT = {"litellm": None}   # dernier état connu de LiteLLM, pour ne journaliser que les changements


# =====================================================================
#  Outil « documents » : NAS en lecture seule (désactivé par défaut)
# =====================================================================
def charger_module_documents():
    spec = importlib.util.spec_from_file_location("pk_documents", Path(__file__).with_name("documents.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pk_documents"] = mod
    spec.loader.exec_module(mod)
    return mod


def demarrer_nas(app: FastAPI) -> None:
    """Prépare l'outil documents. Un échec est journalisé et laisse l'outil éteint : jamais de panne générale."""
    global DOCS, DOCUMENTS
    try:
        DOCUMENTS = charger_module_documents()
        DOCS = DOCUMENTS.Documents(DOCUMENTS.Config.depuis_env())
    except Exception as e:  # noqa: BLE001
        LOG.error("Outil documents NON démarré (%s : %s) — les agents travaillent sans le NAS", type(e).__name__, masquer(str(e))[:200])
        DOCS = None
        return
    LOG.info("Outil documents actif (lecture seule) : NAS %s, index %s", DOCS.racine, DOCS.cfg.index)
    app.state.taches.append(asyncio.create_task(boucle_indexation(app)))


def charger_module_mcp() -> None:
    global MCPM
    if MCPM is None:
        spec = importlib.util.spec_from_file_location("pk_mcp_client", Path(__file__).with_name("mcp_client.py"))
        MCPM = importlib.util.module_from_spec(spec)
        sys.modules["pk_mcp_client"] = MCPM
        spec.loader.exec_module(MCPM)


def demarrer_mcp(app: FastAPI) -> None:
    """Prépare le client MCP. Un échec est journalisé et laisse MCP éteint : jamais de panne générale."""
    global MCPM, MCPC
    try:
        charger_module_mcp()
        MCPC = MCPM.depuis_env(set(REGISTRE.agents), AGENTS_DIR)
    except Exception as e:  # noqa: BLE001
        LOG.error("MCP NON démarré (%s : %s) — les agents travaillent sans outils externes", type(e).__name__, masquer(str(e))[:200])
        MCPC = None
        return
    MCPC.validation_pb = MCP_CLE_PB
    if MCP_CLE_PB:
        LOG.error("MCP : %s Les outils d'écriture (orange) ne sont pas proposés aux agents.", MCP_CLE_PB)
    LOG.info("MCP actif : %d serveur(s), %d outil(s) autorisé(s)%s", len(MCPC.cfg.serveurs), len(MCPC.cfg.outils),
             " — AUCUN outil configuré (agents/mcp.yaml absent ou vide)" if MCPC.cfg.vide else "")
    app.state.taches.append(asyncio.create_task(boucle_mcp()))


async def boucle_mcp() -> None:
    """Entretien de la file de validation : expiration à 24 h, nouvel essai des notifications échouées."""
    while True:
        try:
            await MCPC.entretien()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            LOG.exception("Entretien MCP en échec (réessai dans 5 min)")
        await asyncio.sleep(300)


async def indexer_nas() -> dict:
    bilan = await asyncio.to_thread(DOCS.indexer)
    LOG.info("Indexation du NAS : %s", bilan)
    return bilan


async def boucle_indexation(app: FastAPI) -> None:
    """Indexation au démarrage si l'index est vide, puis chaque nuit à l'heure réglée."""
    try:
        await asyncio.sleep(5)
        if DOCS.etat()["fichiers_indexes"] == 0:
            await indexer_nas()
        while True:
            maintenant = dt.datetime.now(TZ)
            cible = DOCUMENTS.prochaine_execution(maintenant, DOCS.cfg.nuit)
            await asyncio.sleep(max(1.0, (cible - maintenant).total_seconds()))
            await indexer_nas()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        LOG.exception("Boucle d'indexation du NAS arrêtée")


def utilise_outils(agent: dict, corps: dict) -> bool:
    return DOCS is not None and "documents" in (agent.get("outils") or []) and not corps.get("tools")


def utilise_mcp(agent: dict, corps: dict) -> bool:
    """Vrai si MCP est actif ET que la fiche de l'agent déclare `outils: [mcp]` (l'outil « documents » garde la priorité si l'agent a les deux)."""
    return MCPC is not None and "mcp" in (agent.get("outils") or []) and not corps.get("tools") and not utilise_outils(agent, corps)


def demandeur_de(request: Request, corps: dict) -> str:
    """Qui demande (pour le journal des consultations) : utilisateur d'Open WebUI si transmis, sinon champ user, sinon « api »."""
    h = request.headers
    nom, ident = h.get("x-openwebui-user-name", "").strip(), h.get("x-openwebui-user-id", "").strip()
    if nom or ident:
        return f"{nom or '?'} ({ident or '?'})"[:120]
    u = corps.get("user") if isinstance(corps, dict) else None
    return u.strip()[:80] if isinstance(u, str) and u.strip() else "api"


def avec_contexte_repli(corps: dict, contexte: str) -> dict:
    messages = [dict(m) for m in corps["messages"]]
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = f"{messages[0]['content']}\n\n{contexte}"
    else:
        messages.insert(0, {"role": "system", "content": contexte})
    return {**{k: v for k, v in corps.items() if k not in ("tools", "tool_choice")}, "messages": messages}


async def appeler_repli(client, corps, chaine, agent, demandeur):
    """Sans appel d'outils : recherche dans l'index d'après la demande, extraits trouvés injectés dans le contexte."""
    demande = dernier_message_utilisateur(corps["messages"])
    contexte = await asyncio.to_thread(DOCUMENTS.contexte_repli, DOCS, demande, demandeur, agent["id"])
    return await appeler(client, avec_contexte_repli(corps, contexte), chaine)


def verifier_chemins(reponse: dict) -> None:
    """Ajoute un avertissement visible si la réponse cite un nom de document qui n'existe pas dans le NAS."""
    try:
        msg = reponse["choices"][0]["message"]
        contenu = msg.get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return
    inconnus = []
    for nom in DOCUMENTS.chemins_cites(contenu):
        if nom not in inconnus and not DOCS.nom_existe(nom):
            inconnus.append(nom)
    if inconnus:
        LOG.warning("Réponse citant des documents absents du NAS : %s", ", ".join(inconnus[:5]))
        msg["content"] = (contenu + "\n\n⚠️ **Non vérifié dans le NAS** : " + ", ".join(inconnus[:5])
                          + " — ce nom n'a pas été trouvé dans le NAS ; ne pas s'y fier.")


MESSAGE_NAS_INUTILISABLE = ("Je n'ai pas pu consulter le NAS pour cette demande (la réponse du modèle était inexploitable). "
                            "Reformulez la demande ou réessayez dans un instant.")


def assainir_reponse(rep: dict) -> None:
    """Rien de technique ne part vers l'utilisateur : ni balise d'appel d'outil (<tool_call>, <function=…>), ni tool_calls internes."""
    try:
        msg = rep["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return
    if not isinstance(msg, dict):
        return
    contenu = msg.get("content")
    contenu = contenu if isinstance(contenu, str) else ""
    if DOCUMENTS.fuite_balises(contenu):
        LOG.warning("Balises d'appel d'outil retirées de la réponse du modèle")
        contenu = DOCUMENTS.nettoyer_balises(contenu)
    if msg.pop("tool_calls", None):
        LOG.warning("Appel d'outil non traité retiré de la réponse du modèle")
    msg["content"] = contenu if contenu.strip() else MESSAGE_NAS_INUTILISABLE
    if rep["choices"][0].get("finish_reason") == "tool_calls":
        rep["choices"][0]["finish_reason"] = "stop"


MESSAGE_MCP_VIDE = "Je n'ai pas pu utiliser l'outil demandé pour cette réponse. Reformulez la demande ou réessayez dans un instant."


async def appeler_agent_mcp(client: httpx.AsyncClient, corps: dict, chaine: list[str], agent: dict,
                            demandeur: str = "api") -> tuple[dict, str]:
    """Boucle d'appels d'outils MCP. Seuls les VRAIS tool_calls sont traités (jamais un appel écrit en texte) ; tours et appels bornés ;
    les écritures sont mises en attente par le gestionnaire ; le texte renvoyé par un outil est une donnée, pas une consigne."""
    outils = await MCPC.outils_pour(agent["id"])
    if not outils:
        return await appeler(client, corps, chaine)
    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    corps_o = {**corps, "tools": outils, "tool_choice": "auto", "messages": [dict(m) for m in corps["messages"]]}
    appels_faits = 0
    try:
        for _ in range(MCPC.cfg.tours_max):
            rep, cerveau = await appeler(client, corps_o, chaine)
            cumuler_usage(usage, rep)
            msg = rep["choices"][0]["message"]
            appels = [a for a in (msg.get("tool_calls") or []) if isinstance(a, dict)]
            if not appels:
                break
            corps_o["messages"].append({"role": "assistant", "content": msg.get("content"), "tool_calls": appels})
            for appel in appels:
                fonction = appel.get("function") if isinstance(appel.get("function"), dict) else {}
                if appels_faits >= MCPC.cfg.appels_max:
                    resultat = "Trop d'appels d'outils pour cette demande : conclus avec ce que tu as."
                else:
                    appels_faits += 1
                    resultat = await MCPC.executer(agent["id"], demandeur, str(fonction.get("name", "")), fonction.get("arguments"))
                corps_o["messages"].append({"role": "tool", "tool_call_id": str(appel.get("id", "")), "content": resultat})
        else:  # le modèle réclame encore des outils : dernier appel pour qu'il conclue avec ce qu'il a
            corps_o["tool_choice"] = "none"
            rep, cerveau = await appeler(client, corps_o, chaine)
            cumuler_usage(usage, rep)
    except EchecCerveaux as e:
        if e.statut in (400, 422, 501):
            LOG.warning("Le cerveau n'accepte pas l'appel d'outils MCP (%s) : réponse sans outils", e.statut)
            rep, cerveau = await appeler(client, corps, chaine)
            cumuler_usage(usage, rep)
        else:
            raise
    if usage["total_tokens"]:
        rep["usage"] = usage
    try:                                                    # rien de technique ne part vers l'utilisateur
        m = rep["choices"][0]["message"]
        m.pop("tool_calls", None)
        propre = MCPM.nettoyer_texte(m.get("content") if isinstance(m.get("content"), str) else "", 100000)[0]
        m["content"] = propre if propre.strip() else MESSAGE_MCP_VIDE
        if rep["choices"][0].get("finish_reason") == "tool_calls":
            rep["choices"][0]["finish_reason"] = "stop"
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    return rep, cerveau


async def appeler_agent(client: httpx.AsyncClient, corps: dict, chaine: list[str], agent: dict,
                        demandeur: str = "api") -> tuple[dict, str]:
    """Comme appeler(), avec l'outil « documents » si l'agent y a droit et que le NAS est actif (sinon : strictement appeler())."""
    if utilise_mcp(agent, corps):
        return await appeler_agent_mcp(client, corps, chaine, agent, demandeur)
    if not utilise_outils(agent, corps):
        return await appeler(client, corps, chaine)
    mode = DOCS.cfg.mode
    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if mode == "repli":
        rep, cerveau = await appeler_repli(client, corps, chaine, agent, demandeur)
        cumuler_usage(usage, rep)
    else:
        corps_o = {**corps, "tools": DOCUMENTS.OUTILS_OPENAI, "tool_choice": "auto", "messages": [dict(m) for m in corps["messages"]]}
        try:
            for _ in range(DOCS.cfg.tours_max):
                rep, cerveau = await appeler(client, corps_o, chaine)
                cumuler_usage(usage, rep)
                msg = rep["choices"][0]["message"]
                appels = msg.get("tool_calls") or []
                contenu = msg.get("content") if isinstance(msg.get("content"), str) else ""
                if appels:
                    contenu_assistant = msg.get("content")
                else:
                    contenu_assistant = None
                    if DOCUMENTS.fuite_balises(contenu):
                        # certains modèles écrivent l'appel d'outil en TEXTE au lieu d'un vrai tool_call : on l'exécute quand même
                        appels = DOCUMENTS.appels_en_texte(contenu)
                        if appels:
                            LOG.warning("Appel d'outil écrit en texte par le modèle (%d) : exécuté comme un vrai appel", len(appels))
                        else:
                            LOG.warning("Appel d'outil illisible dans la réponse du modèle : repli sur la recherche dans l'index")
                            rep, cerveau = await appeler_repli(client, corps, chaine, agent, demandeur)
                            cumuler_usage(usage, rep)
                            break
                if not appels:
                    break
                corps_o["messages"].append({"role": "assistant", "content": contenu_assistant, "tool_calls": appels})
                for appel in appels:
                    fonction = appel.get("function") or {}
                    resultat = await asyncio.wait_for(
                        asyncio.to_thread(DOCS.executer, str(fonction.get("name", "")), fonction.get("arguments"), demandeur, agent["id"]),
                        DOCS.cfg.delai + 5)
                    corps_o["messages"].append({"role": "tool", "tool_call_id": appel.get("id", ""), "content": resultat})
            else:  # le modèle réclame encore des outils : dernier appel pour qu'il conclue avec ce qu'il a
                corps_o["tool_choice"] = "none"
                rep, cerveau = await appeler(client, corps_o, chaine)
                cumuler_usage(usage, rep)
        except EchecCerveaux as e:
            if mode == "auto" and e.statut in (400, 422, 501):
                LOG.warning("Le cerveau n'accepte pas l'appel d'outils (%s) : repli sur la recherche dans l'index", e.statut)
                rep, cerveau = await appeler_repli(client, corps, chaine, agent, demandeur)
                cumuler_usage(usage, rep)
            else:
                raise
        except asyncio.TimeoutError:
            raise EchecCerveaux(504, "la lecture du NAS a dépassé le délai", [agent["cerveau"]]) from None
    if usage["total_tokens"]:
        rep["usage"] = usage
    assainir_reponse(rep)
    verifier_chemins(rep)
    return rep, cerveau


# =====================================================================
#  Application
# =====================================================================
@asynccontextmanager
async def cycle_de_vie(app: FastAPI):
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, connect=10))
    if not API_KEY:
        LOG.error("Aucune clé (PK_API_KEY / LITELLM_MASTER_KEY) : l'API reste FERMÉE tant qu'elle n'est pas définie")
    await JOURNAL.demarrer()
    app.state.taches = []
    if NAS_ACTIF:
        demarrer_nas(app)
    if MCP_ACTIF:
        demarrer_mcp(app)
    yield
    for t in getattr(app.state, "taches", []):
        t.cancel()
    if MCPC is not None:
        await MCPC.fermer()
    await app.state.client.aclose()
    await JOURNAL.arreter()


app = FastAPI(title=f"Bureau IA {MARQUE} — Orchestrateur", version=VERSION, lifespan=cycle_de_vie,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(Exception)
async def erreur_inattendue(request: Request, exc: Exception):
    LOG.error("Erreur interne sur %s %s", request.method, request.url.path, exc_info=exc)
    return erreur(500, "Erreur interne de l'orchestrateur (détails dans les journaux du serveur)", "interne")


def autorise(request: Request, cle: str) -> bool:
    """Clé exigée (jamais d'accès libre), comparée à temps constant."""
    if not cle:
        return False
    auth = request.headers.get("authorization", "")
    fourni = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
    return bool(fourni) and hmac.compare_digest(fourni.encode("utf-8"), cle.encode("utf-8"))


@app.get("/health")
async def sante(request: Request):
    REGISTRE.recharger()
    litellm_ok = False
    try:
        r = await request.app.state.client.get(f"{LITELLM_URL}/health/liveliness", timeout=5)
        litellm_ok = r.status_code == 200
        if not litellm_ok and ETAT["litellm"] is not False:
            LOG.warning("LiteLLM injoignable depuis /health : HTTP %s", r.status_code)
    except httpx.HTTPError as e:
        if ETAT["litellm"] is not False:   # une seule ligne par panne, pas une toutes les 15 s
            LOG.warning("LiteLLM injoignable depuis /health (%s)", type(e).__name__)
    if litellm_ok and ETAT["litellm"] is False:
        LOG.info("LiteLLM de nouveau joignable")
    ETAT["litellm"] = litellm_ok
    sortie = {"ok": bool(REGISTRE.agents), "version": VERSION, "agents": len(REGISTRE.agents),
              "litellm": litellm_ok, "avertissements": REGISTRE.avertissements}
    if NAS_ACTIF:   # clé ajoutée seulement quand l'outil est demandé : /health reste identique pour PixelKréol
        dispo = False
        if DOCS is not None:
            try:
                with os.scandir(DOCS.racine) as it:
                    dispo = next(it, None) is not None       # partage monté et non vide
            except OSError:
                dispo = False
        sortie["nas"] = {"actif": DOCS is not None, "disponible": dispo}
    if MCP_SERVEUR_ACTIF:   # idem pour le serveur MCP
        sortie["mcp_serveur"] = {"actif": MCPSRV is not None, "agents": len(MCPSRV.cfg.agents) if MCPSRV is not None else 0,
                                 "disponible": MCPSRV is not None and MCP_SERVEUR_PB is None, "raison": MCP_SERVEUR_PB or ""}
    if MCP_ACTIF:   # clé ajoutée seulement quand MCP est demandé : /health reste identique sinon
        sortie["mcp"] = {"actif": MCPC is not None, "outils": len(MCPC.cfg.outils) if MCPC is not None else 0,
                         "validation": {"disponible": MCP_CLE_PB is None, "raison": MCP_CLE_PB or ""}}
    return sortie


@app.get("/v1/models")
async def modeles(request: Request):
    if not autorise(request, API_KEY):
        return erreur(401, "Clé API invalide", "auth")
    REGISTRE.recharger()
    cree = int(time.time())
    donnees = [
        {"id": "pk-auto", "object": "model", "created": cree, "owned_by": PROPRIETAIRE,
         "name": "🧠 PK Auto — choisit le bon agent"},
        {"id": "pk-equipe", "object": "model", "created": cree, "owned_by": PROPRIETAIRE,
         "name": "👥 PK Équipe — plusieurs agents + synthèse"},
    ]
    for a in REGISTRE.agents.values():
        donnees.append({"id": a["modele"], "object": "model", "created": cree, "owned_by": PROPRIETAIRE,
                        "name": f"{a['emoji']} {a['nom']}", "description": a["description"],
                        "famille": a["famille"], "cerveau": a["cerveau"]})
    return {"object": "list", "data": donnees}


@app.get("/agents")
async def liste_agents(request: Request):
    if not autorise(request, API_KEY):
        return erreur(401, "Clé API invalide", "auth")
    REGISTRE.recharger()
    return {"total": len(REGISTRE.agents), "avertissements": REGISTRE.avertissements,
            "agents": [{k: v for k, v in a.items() if not k.startswith("_")} for a in REGISTRE.agents.values()]}


@app.get("/stats")
async def stats(request: Request):
    if not autorise(request, API_KEY):
        return erreur(401, "Clé API invalide", "auth")
    return {"par_agent": JOURNAL.compteurs, "journal_postgres": JOURNAL.pool is not None}


# =====================================================================
#  Activité : qui demande quoi, quel maillon répond (lecture protégée par la clé admin, jamais publique)
# =====================================================================
def _iso(v: Any) -> str:
    try:
        return v.astimezone(TZ).isoformat(timespec="seconds")
    except AttributeError:
        return str(v or "")


def _filtre(valeur: str, defaut: str = "") -> str:
    """Valeur de filtre venue d'une requête : courte, sans caractères de contrôle (elle n'est utilisée qu'en paramètre SQL)."""
    return "".join(ch for ch in (valeur or defaut)[:120] if ch.isprintable())


@app.get("/admin/activite")
async def activite(request: Request, agent: str = "", demandeur: str = "", limite: int = 100):
    if not autorise(request, ADMIN_KEY):
        return erreur(401, "Clé admin invalide", "auth")
    limite = max(1, min(int(limite), 500))
    agent, demandeur = _filtre(agent), _filtre(demandeur)
    sortie: dict[str, Any] = {"sujet_actif": SUJET_ACTIF, "journal_postgres": JOURNAL.pool is not None, "marque": MARQUE,
                              "en_cours": JOURNAL.demandes_en_cours(), "dernieres": [], "compteurs_jour": [],
                              "agents": [], "personnes": [], "maillons": [], "jours_conserves": JOURNAL_JOURS,
                              "abandons": JOURNAL.abandons}
    if JOURNAL.pool is None:
        sortie["compteurs_memoire"] = JOURNAL.compteurs
        return sortie
    minuit = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        async with JOURNAL.pool.acquire() as con:
            lignes = await asyncio.wait_for(con.fetch(
                """SELECT ts, agent, mode, cerveau, fournisseur, demandeur, sujet, duree_ms, ok, detail FROM pk_journal
                   WHERE ($1::text = '' OR agent = $1::text) AND ($2::text = '' OR demandeur = $2::text) ORDER BY ts DESC LIMIT $3""",
                agent, demandeur, limite), 10)
            sortie["dernieres"] = [{"ts": _iso(r["ts"]), "agent": r["agent"], "mode": r["mode"], "cerveau": r["cerveau"],
                                    "fournisseur": r["fournisseur"] or "", "demandeur": r["demandeur"] or "",
                                    "sujet": r["sujet"] or "", "duree_ms": r["duree_ms"], "ok": r["ok"],
                                    "detail": r["detail"] or ""} for r in lignes]
            jour = await asyncio.wait_for(con.fetch(
                "SELECT agent, count(*) AS n, count(*) FILTER (WHERE NOT ok) AS echecs FROM pk_journal WHERE ts >= $1 GROUP BY agent ORDER BY n DESC",
                minuit), 10)
            sortie["compteurs_jour"] = [{"agent": r["agent"], "total": r["n"], "echecs": r["echecs"]} for r in jour]
            sortie["agents"] = [r["agent"] for r in await con.fetch(
                "SELECT DISTINCT agent FROM pk_journal WHERE ts > now() - interval '30 days' AND agent IS NOT NULL ORDER BY agent LIMIT 100")]
            sortie["personnes"] = [r["demandeur"] for r in await con.fetch(
                "SELECT DISTINCT demandeur FROM pk_journal WHERE ts > now() - interval '30 days' AND demandeur <> '' ORDER BY demandeur LIMIT 100")]
            sortie["maillons"] = await _sante_maillons(con)
    except Exception as e:  # noqa: BLE001 — une panne de base ne casse pas la page
        LOG.warning("activité : lecture impossible (%s)", masquer(str(e) or type(e).__name__)[:200])
        sortie["erreur"] = "Lecture du journal impossible pour le moment"
    return sortie


async def _sante_maillons(con) -> list[dict]:
    """Santé de chaque maillon d'après les appels réels (et les tests lancés à la main) ; rouge = 402/404 depuis plus de 24 h."""
    dernier = await con.fetch("""SELECT DISTINCT ON (maillon) maillon, ts, statut, resultat, source, fournisseur FROM pk_maillons
                                 WHERE ts > now() - interval '30 days' ORDER BY maillon, ts DESC""")
    dernier_ok = {r["maillon"]: r for r in await con.fetch(
        """SELECT DISTINCT ON (maillon) maillon, ts, fournisseur FROM pk_maillons
           WHERE statut = 200 AND ts > now() - interval '30 days' ORDER BY maillon, ts DESC""")}
    j24 = {r["maillon"]: r for r in await con.fetch(
        """SELECT maillon, count(*) AS appels, count(*) FILTER (WHERE statut <> 200) AS echecs FROM pk_maillons
           WHERE source = 'trafic' AND ts > now() - interval '24 hours' GROUP BY maillon""")}
    depuis = {r["maillon"]: r["depuis"] for r in await con.fetch(
        """SELECT m.maillon, min(m.ts) AS depuis FROM pk_maillons m
           WHERE m.statut IN (402, 404) AND m.ts > COALESCE((SELECT max(x.ts) FROM pk_maillons x WHERE x.maillon = m.maillon AND x.statut = 200), '-infinity')
           GROUP BY m.maillon""")}
    limite = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    sortie = []
    for r in dernier:
        m = r["maillon"]
        d = depuis.get(m)
        mort = r["statut"] in (402, 404) and d is not None and d < limite
        ok = dernier_ok.get(m)
        sortie.append({"maillon": m, "dernier": {"ts": _iso(r["ts"]), "resultat": r["resultat"], "statut": r["statut"], "source": r["source"]},
                       "fournisseur": (ok["fournisseur"] if ok else "") or r["fournisseur"] or "",
                       "appels_24h": (j24.get(m) or {}).get("appels", 0), "echecs_24h": (j24.get(m) or {}).get("echecs", 0),
                       "dernier_ok": _iso(ok["ts"]) if ok else "", "mort": mort, "depuis": _iso(d) if (d and r["statut"] in (402, 404)) else ""})
    sortie.sort(key=lambda x: (not x["mort"], x["maillon"]))
    return sortie


TEST_VERROU = asyncio.Lock()


@app.post("/admin/activite/tester")
async def tester_maillons(request: Request):
    """« Tester maintenant » : UN mot à chaque fournisseur de LiteLLM, un par un et sans bascule (comme verifier.sh).
    Lancé à la main seulement : ça consomme un peu des quotas gratuits. Le résultat alimente la santé des maillons."""
    if not autorise(request, ADMIN_KEY):
        return erreur(401, "Clé admin invalide", "auth")
    if TEST_VERROU.locked():
        return erreur(409, "Un test est déjà en cours", "occupe")
    async with TEST_VERROU:
        client: httpx.AsyncClient = request.app.state.client
        try:
            r = await client.get(f"{LITELLM_URL}/v1/models", headers=entetes_litellm(), timeout=15)
            maillons = sorted(m["id"] for m in r.json()["data"] if isinstance(m, dict) and isinstance(m.get("id"), str))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            return erreur(502, f"Liste des cerveaux illisible ({type(e).__name__})", "cerveaux_indisponibles")
        feu = asyncio.Semaphore(4)

        async def un(maillon: str) -> dict:
            async with feu:
                t0 = time.monotonic()
                modele, statut = "", 0
                try:
                    rep = await client.post(f"{LITELLM_URL}/v1/chat/completions", headers=entetes_litellm(), timeout=45, json={
                        "model": maillon, "max_tokens": 8, "temperature": 0, "disable_fallbacks": True,
                        "messages": [{"role": "user", "content": "Réponds seulement : OK"}]})
                    statut = rep.status_code
                    if statut == 200:
                        try:
                            modele = fournisseur_reel(str(rep.json().get("model") or ""), rep.headers, client)
                        except ValueError:
                            pass
                except httpx.HTTPError as e:
                    statut = 504 if isinstance(e, httpx.TimeoutException) else 503
                ms = int((time.monotonic() - t0) * 1000)
                JOURNAL.maillon(maillon, statut, modele, "test", ms)
                return {"maillon": maillon, "statut": statut, "resultat": statut_maillon(statut), "fournisseur": masquer(modele)[:80], "ms": ms}

        resultats = await asyncio.gather(*[un(m) for m in maillons])
        bons = sum(1 for x in resultats if x["statut"] == 200)
        return {"ok": bons == len(resultats), "bons": bons, "total": len(resultats), "resultats": resultats}


@app.post("/route")
async def tester_routage(request: Request):
    """Dit quel agent pk-auto choisirait, sans appeler de modèle (sauf routage IA en cas de doute)."""
    if not autorise(request, API_KEY):
        return erreur(401, "Clé API invalide", "auth")
    try:
        corps = await request.json()
    except ValueError:
        return erreur(400, "Corps JSON invalide", "requete")
    if not isinstance(corps, dict):
        return erreur(400, "Un objet JSON est attendu", "requete")
    messages = messages_valides(corps) or [{"role": "user", "content": str(corps.get("texte", ""))}]
    vide = aucun_agent()
    if vide:
        return vide
    agent, methode = await choisir_agent(request.app.state.client, messages)
    return {"agent": agent["id"], "modele": agent["modele"], "nom": agent["nom"], "methode": methode}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    if not autorise(request, API_KEY):
        return erreur(401, "Clé API invalide", "auth")
    try:
        corps = await request.json()
    except ValueError:  # JSON invalide ou octets illisibles
        return erreur(400, "Corps JSON invalide", "requete")
    if (not isinstance(corps, dict) or not isinstance(corps.get("messages"), list) or not corps["messages"]
            or not all(isinstance(m, dict) for m in corps["messages"])):
        return erreur(400, "Le champ « messages » (liste non vide d'objets) est obligatoire", "requete")
    client: httpx.AsyncClient = request.app.state.client
    modele = str(corps.get("model", "pk-auto"))
    flux = bool(corps.get("stream"))
    vide = aucun_agent()
    if vide:
        LOG.error("Requête refusée (%s) : aucun agent chargé", modele)
        return vide

    if modele in ("pk-equipe", "equipe"):
        return await mode_equipe(client, corps, flux, demandeur_de(request, corps))

    annonce = ""
    mode = "direct"
    if modele in ("pk-auto", "auto", ""):
        agent, methode = await choisir_agent(client, corps["messages"])
        mode = f"auto:{methode}"
        if ANNONCE:
            annonce = f"*➜ {agent['emoji']} {agent['nom']}*\n\n"
    else:
        agent = REGISTRE.trouver(modele)
        if agent is None:
            return erreur(404, f"Agent « {modele} » inconnu. Voir /v1/models", "introuvable")

    corps_final, chaine = preparer_corps(corps, agent)
    debut = time.monotonic()
    ouvrir_demande(demandeur_de(request, corps), corps, agent["id"], mode)
    JOURNAL.purge_due()
    avec_outils = utilise_outils(agent, corps_final) or utilise_mcp(agent, corps_final)

    if not flux or avec_outils:
        try:
            if avec_outils:
                reponse, cerveau = await appeler_agent(client, corps_final, chaine, agent, demandeur_de(request, corps))
            else:
                reponse, cerveau = await appeler(client, corps_final, chaine)
        except EchecCerveaux as e:
            await JOURNAL.ecrire(agent["id"], mode, "-", debut, False, e.detail)
            return erreur(e.statut if e.statut >= 400 else 502, e.message(agent["nom"]), "cerveaux_indisponibles")
        reponse["model"] = agent["modele"]
        if annonce and reponse.get("choices"):
            msg = reponse["choices"][0].setdefault("message", {})
            msg["content"] = annonce + (msg.get("content") or "")
        await JOURNAL.ecrire(agent["id"], mode, cerveau, debut, True)
        if flux:  # l'agent a travaillé avec l'outil documents : la réponse complète est envoyée en flux, d'un bloc
            texte = texte_de_la_reponse(reponse)
            return StreamingResponse(flux_texte(agent["modele"], texte), media_type="text/event-stream",
                                     headers={"X-PK-Agent": agent["id"], "X-PK-Cerveau": cerveau,
                                              "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        return JSONResponse(reponse, headers={"X-PK-Agent": agent["id"], "X-PK-Cerveau": cerveau})

    try:
        amont, cerveau, premier, suite, avert = await ouvrir_flux(client, corps_final, chaine)
    except EchecCerveaux as e:
        await JOURNAL.ecrire(agent["id"], mode, "-", debut, False, e.detail)
        return erreur(e.statut if e.statut >= 400 else 502, e.message(agent["nom"]), "cerveaux_indisponibles")

    async def relais() -> AsyncIterator[bytes]:
        ok = True
        detail = ""
        try:
            if annonce:
                yield morceau_sse(agent["modele"], annonce)
            if avert:
                yield morceau_sse(agent["modele"], avert)
            fin_vue = b""
            try:
                yield premier
                fin_vue = premier[-32:]
                async for bloc in suite:
                    fin_vue = (fin_vue + bloc)[-32:]
                    yield bloc
                if b"[DONE]" not in fin_vue:
                    ok, detail = False, "flux terminé sans [DONE]"
            except httpx.HTTPError as e:
                ok, detail = False, f"flux coupé ({type(e).__name__})"
            if not ok:
                LOG.warning("flux interrompu (agent %s, cerveau %s) : %s", agent["id"], cerveau, detail)
                cid = f"chatcmpl-pk-{uuid.uuid4().hex[:12]}"
                yield morceau_sse(agent["modele"], "\n\n⚠️ *Réponse interrompue : la connexion avec le cerveau "
                                  f"({cerveau}) s'est coupée. Relance ta demande pour obtenir la suite.*", cid=cid)
                yield morceau_sse(agent["modele"], fin="stop", cid=cid)
                yield b"data: [DONE]\n\n"
        finally:
            await amont.aclose()
            await JOURNAL.ecrire(agent["id"], mode, cerveau, debut, ok, detail)

    return StreamingResponse(relais(), media_type="text/event-stream",
                             headers={"X-PK-Agent": agent["id"], "X-PK-Cerveau": cerveau,
                                      "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# =====================================================================
#  Mode équipe : planification → exécution parallèle → synthèse
# =====================================================================
def extraire_json(texte: str) -> dict | None:
    texte = re.sub(r"```(?:json)?", "", texte or "")
    debut = texte.find("{")
    while debut != -1:
        profondeur = 0
        for i in range(debut, len(texte)):
            if texte[i] == "{":
                profondeur += 1
            elif texte[i] == "}":
                profondeur -= 1
                if profondeur == 0:
                    try:
                        return json.loads(texte[debut:i + 1])
                    except json.JSONDecodeError:
                        break
        debut = texte.find("{", debut + 1)
    return None


async def executer_agent(client: httpx.AsyncClient, agent: dict, messages: list[dict],
                         usage: dict | None = None, demandeur: str = "api") -> tuple[str, bool]:
    """Fait travailler un agent. Ne lève jamais d'exception : renvoie (texte, réussi)."""
    corps, chaine = preparer_corps({"messages": messages, "max_tokens": 1200}, agent)
    debut = time.monotonic()
    base = CTX_DEMANDE.get()
    if base is not None:      # une copie par agent (travail en parallèle) : son propre fournisseur, sans toucher aux demandes « en cours »
        CTX_DEMANDE.set({**base, "id": "", "fournisseur": ""})
    try:
        rep, cerveau = await asyncio.wait_for(appeler_agent(client, corps, chaine, agent, demandeur), DELAI_AGENT)
        cumuler_usage(usage, rep)
        texte = texte_de_la_reponse(rep)
        await JOURNAL.ecrire(agent["id"], "equipe", cerveau, debut, bool(texte.strip()))
        return (texte, True) if texte.strip() else ("(indisponible : réponse sans texte)", False)
    except EchecCerveaux as e:
        await JOURNAL.ecrire(agent["id"], "equipe", "-", debut, False, e.detail)
        return f"(indisponible : {e.detail[:120]})", False
    except asyncio.TimeoutError:
        LOG.warning("pk-equipe : %s n'a pas répondu en %s s", agent["id"], DELAI_AGENT)
        await JOURNAL.ecrire(agent["id"], "equipe", "-", debut, False, f"délai de {DELAI_AGENT} s dépassé")
        return f"(indisponible : délai de {DELAI_AGENT:.0f} s dépassé)", False
    except Exception as e:  # noqa: BLE001 — un agent en difficulté ne doit pas faire perdre le travail des autres
        LOG.exception("pk-equipe : erreur inattendue avec l'agent %s", agent["id"])
        await JOURNAL.ecrire(agent["id"], "equipe", "-", debut, False, type(e).__name__)
        return "(indisponible : erreur interne)", False


def choisir_taches(plan: Any, demande: str, ids: list[str]) -> list[tuple[dict, str]]:
    """Tâches du plan du directeur (agents connus, sans doublon, au plus MAX_EQUIPE)."""
    brutes = plan.get("taches") if isinstance(plan, dict) else None
    taches: list[tuple[dict, str]] = []
    vus: set[str] = set()
    for t in brutes if isinstance(brutes, list) else []:
        if len(taches) >= MAX_EQUIPE:
            break
        ident = t.get("agent") if isinstance(t, dict) else None
        if not isinstance(ident, str) or ident in vus or ident == "orchestrateur" or ident not in REGISTRE.agents:
            continue
        vus.add(ident)
        taches.append((REGISTRE.agents[ident], str(t.get("consigne") or demande)))
    if not taches:  # plan illisible : on se rabat sur les meilleurs agents par mots-clés
        notes = [n for n in classement_mots_cles(demande) if n[1]["id"] != "orchestrateur"]
        taches = [(a, demande) for s, a in notes[:2] if s > 0] or [(REGISTRE.agents[ids[0]], demande)]
    return taches


async def mode_equipe(client: httpx.AsyncClient, corps: dict, flux: bool, demandeur: str = "api"):
    ctx = ouvrir_demande(demandeur, corps, "pk-equipe", "equipe")
    try:
        return await _mode_equipe(client, corps, flux, demandeur)
    finally:
        JOURNAL.en_cours.pop(ctx["id"], None)


async def _mode_equipe(client: httpx.AsyncClient, corps: dict, flux: bool, demandeur: str = "api"):
    messages = [m for m in corps["messages"] if isinstance(m, dict) and m.get("role") != "system"]
    demande = dernier_message_utilisateur(messages)
    directeur = REGISTRE.agents.get("orchestrateur")
    ids = [a for a in REGISTRE.agents if a != "orchestrateur"]
    if not directeur or not ids:
        return erreur(500, "Le mode équipe demande l'agent « orchestrateur » et au moins un autre agent", "config")

    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    consigne_plan = (
        "PLANIFICATION_JSON. Découpe la demande en 1 à "
        f"{MAX_EQUIPE} tâches confiées aux agents les plus adaptés. Réponds UNIQUEMENT avec un objet JSON "
        '{"taches":[{"agent":"<id>","consigne":"<ce que cet agent doit produire>"}]}. '
        f"Identifiants autorisés : {', '.join(ids)}.\n\nDemande : {demande}")
    plan_txt, ok = await executer_agent(client, directeur, [{"role": "user", "content": consigne_plan}], usage, demandeur)
    plan = extraire_json(plan_txt) if ok else None
    taches = choisir_taches(plan, demande, ids)
    notes: list[str] = []
    if plan is None:
        notes.append("Le découpage automatique n'a pas fonctionné : agents choisis d'après les mots-clés de ta demande.")
        LOG.warning("pk-equipe : plan du directeur inutilisable (%s)", "directeur en panne" if not ok else "JSON illisible")

    historique = messages[:-1][-6:]
    resultats = await asyncio.gather(*[
        executer_agent(client, a, historique + [{"role": "user", "content": f"{c}\n\n(Demande d'origine : {demande})"}], usage, demandeur)
        for a, c in taches])

    muets = [a for (a, _), (_, reussi) in zip(taches, resultats) if not reussi]
    if len(muets) == len(taches):
        return erreur(502, "Aucun agent de l'équipe n'a pu répondre (" + ", ".join(a["nom"] for a in muets)
                      + "). Réessaie dans quelques minutes.", "cerveaux_indisponibles")

    contributions = "\n\n".join(f"### {a['emoji']} {a['nom']}\n{txt}" for (a, _), (txt, _) in zip(taches, resultats))
    prompt_synthese = (f"Demande de {PATRON} : {demande}\n\nVoici les contributions de l'équipe :\n\n{contributions}\n\n")
    if muets:
        prompt_synthese += ("ATTENTION : " + ", ".join(a["nom"] for a in muets) + " n'a pas pu répondre (indisponible). "
                            "N'invente pas leur partie : dis clairement ce qui manque et ce qu'il faudra redemander.\n\n")
    prompt_synthese += "Fais la synthèse finale : réponse directe et exploitable, puis les actions et qui s'en charge."
    synthese_txt, ok_s = await executer_agent(client, directeur, [{"role": "user", "content": prompt_synthese}], usage, demandeur)

    entete = "*👥 Équipe mobilisée : " + ", ".join(
        f"{a['emoji']} {a['nom']}" + (" ⚠️" if a in muets else "") for a, _ in taches) + "*\n\n"
    if muets:
        entete += ("⚠️ **Réponse incomplète** — sans réponse de : " + ", ".join(a["nom"] for a in muets)
                   + " (panne ou délai dépassé). La synthèse ne couvre pas leur partie.\n\n")
    for n in notes:
        entete += f"*{n}*\n\n"
    if ok_s:
        final = entete + synthese_txt + "\n\n<details><summary>Contributions détaillées</summary>\n\n" + contributions + "\n\n</details>"
    else:
        final = entete + "La synthèse n'a pas pu être faite. Contributions brutes :\n\n" + contributions
    if flux:
        return StreamingResponse(flux_texte("pk-equipe", final), media_type="text/event-stream",
                                 headers={"X-PK-Agent": "equipe"})
    return JSONResponse(completion_simple("pk-equipe", final, usage), headers={"X-PK-Agent": "equipe"})


# =====================================================================
#  Administration : ajouter / retirer un agent personnalisé à chaud
# =====================================================================
TAILLE_MAX_ADMIN = 64 * 1024


class PersoIllisible(Exception):
    """perso.yaml existe mais n'est pas lisible : on refuse d'y écrire pour ne rien écraser."""


def ecrire_perso(agents: list[dict]) -> None:
    chemin = AGENTS_DIR / "perso.yaml"
    contenu = ("# Agents personnalisés (créés via /admin/agents ou la Fabrique d'agents).\n"
               "# Même format que registre.yaml. Rechargé à chaud.\n"
               + yaml.safe_dump({"agents": agents}, sort_keys=False, allow_unicode=True))
    fd, tmp = tempfile.mkstemp(dir=str(AGENTS_DIR), prefix=".perso-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(contenu)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, chemin)  # remplacement atomique : on voit l'ancien fichier ou le nouveau, jamais un demi-fichier
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as e:
            LOG.warning("fichier temporaire %s non supprimé : %s", tmp, e)
        raise


def lire_perso() -> list[dict]:
    chemin = AGENTS_DIR / "perso.yaml"
    if not chemin.exists():
        return []
    try:
        data = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError, ValueError) as e:
        raise PersoIllisible(str(e)[:200]) from e
    if not isinstance(data, dict):
        raise PersoIllisible("le fichier doit commencer par « agents: »")
    agents = data.get("agents") or []
    if not isinstance(agents, list):
        raise PersoIllisible("« agents » doit être une liste")
    return agents


def msg_perso_illisible(e: PersoIllisible) -> str:
    return ("Le fichier agents/perso.yaml est illisible (" + str(e) + "). Rien n'a été modifié : "
            "corrigez-le (ou renommez-le) puis recommencez.")


@app.post("/admin/agents")
async def ajouter_agent(request: Request):
    if not autorise(request, ADMIN_KEY):
        return erreur(401, "Clé admin invalide", "auth")
    octets = await request.body()
    if len(octets) > TAILLE_MAX_ADMIN:
        return erreur(413, f"Corps trop gros (maximum {TAILLE_MAX_ADMIN // 1024} Ko)", "requete")
    try:
        brut = octets.decode("utf-8")
    except UnicodeDecodeError:
        return erreur(400, "Le corps doit être du texte UTF-8", "requete")
    try:
        data = json.loads(brut)
    except ValueError:
        try:
            data = yaml.safe_load(re.sub(r"```(?:yaml)?", "", brut))
        except yaml.YAMLError as e:
            return erreur(400, f"Ni JSON ni YAML valide : {str(e)[:200]}", "requete")
    if isinstance(data, dict) and "agents" in data:
        data = data["agents"] if data["agents"] is not None else []
    if isinstance(data, list):
        if len(data) > 1:
            return erreur(422, "Un seul agent à la fois (plusieurs reçus) : envoyez-les un par un", "validation")
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return erreur(400, "Un agent (objet) est attendu", "requete")
    REGISTRE.recharger()
    try:
        existants = lire_perso()
    except PersoIllisible as e:
        return erreur(409, msg_perso_illisible(e), "conflit")
    if data.get("num") in (None, ""):  # numérotation automatique à partir de 101 pour les agents perso
        data["num"] = max([a["num"] for a in REGISTRE.agents.values()] + [100]) + 1
    pb = Registre.valider(data, set(REGISTRE.agents), {a["num"] for a in REGISTRE.agents.values()})
    if pb:
        return erreur(422, f"Agent refusé : {pb}", "validation")
    propre = {k: v for k, v in data.items() if not str(k).startswith("_") and k not in ("source", "modele")}
    try:
        ecrire_perso(existants + [propre])
    except OSError as e:
        LOG.error("Écriture de perso.yaml impossible : %s", e)
        return erreur(500, "Écriture de perso.yaml impossible (disque plein ou droits). Rien n'a été modifié.", "config")
    REGISTRE.recharger(force=True)
    a = REGISTRE.agents.get(propre["id"])
    if not a:
        return erreur(500, "Agent écrit mais non rechargé : " + "; ".join(REGISTRE.avertissements), "config")
    return JSONResponse(status_code=201, content={"ok": True, "modele": a["modele"], "agent": propre["id"]})


@app.delete("/admin/agents/{agent_id}")
async def retirer_agent(agent_id: str, request: Request):
    if not autorise(request, ADMIN_KEY):
        return erreur(401, "Clé admin invalide", "auth")
    try:
        perso = lire_perso()
    except PersoIllisible as e:
        return erreur(409, msg_perso_illisible(e), "conflit")
    restants = [a for a in perso if not (isinstance(a, dict) and a.get("id") == agent_id)]
    if len(restants) == len(perso):
        return erreur(404, "Seuls les agents de perso.yaml peuvent être retirés par l'API", "introuvable")
    try:
        ecrire_perso(restants)
    except OSError as e:
        LOG.error("Écriture de perso.yaml impossible : %s", e)
        return erreur(500, "Écriture de perso.yaml impossible. Rien n'a été modifié.", "config")
    REGISTRE.recharger(force=True)
    return {"ok": True, "retire": agent_id}


@app.get("/admin/mcp/etat")
async def etat_mcp(request: Request):
    if not autorise(request, ADMIN_KEY):
        return erreur(401, "Clé admin invalide", "auth")
    if MCPC is None:
        return erreur(404, "MCP n'est pas activé", "introuvable")
    return await asyncio.to_thread(MCPC.etat)


@app.get("/admin/mcp/journal")
async def journal_mcp(request: Request):
    if not autorise(request, ADMIN_KEY):
        return erreur(401, "Clé admin invalide", "auth")
    if MCPC is None:
        return erreur(404, "MCP n'est pas activé", "introuvable")
    return {"appels": await asyncio.to_thread(MCPC.base.appels, 100)}


@app.get("/admin/nas")
async def etat_nas(request: Request):
    if not autorise(request, ADMIN_KEY):
        return erreur(401, "Clé admin invalide", "auth")
    if DOCS is None:
        return erreur(404, "L'outil documents (NAS) n'est pas activé", "introuvable")
    return await asyncio.to_thread(DOCS.etat)


@app.post("/admin/nas/reindexer")
async def reindexer_nas(request: Request):
    """Lance une mise à jour de l'index en arrière-plan (à la demande, en plus de celle de la nuit)."""
    if not autorise(request, ADMIN_KEY):
        return erreur(401, "Clé admin invalide", "auth")
    if DOCS is None:
        return erreur(404, "L'outil documents (NAS) n'est pas activé", "introuvable")
    if DOCS.verrou.locked():
        return erreur(409, "Une indexation est déjà en cours", "conflit")
    request.app.state.taches.append(asyncio.create_task(indexer_nas()))
    return JSONResponse(status_code=202, content={"ok": True, "message": "Indexation lancée en arrière-plan : suivre avec GET /admin/nas"})


# =====================================================================
#  Vitrine : page publique listant l'équipe (sans les fiches de poste)
# =====================================================================
@app.get("/", response_class=HTMLResponse)
async def vitrine():
    REGISTRE.recharger()
    cartes = "".join(
        f'<div class="c"><div class="e">{html.escape(a["emoji"])}</div><b>{html.escape(a["nom"])}</b>'
        f'<small>{html.escape(a["famille"])}</small><p>{html.escape(a["description"])}</p></div>'
        for a in REGISTRE.agents.values())
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bureau IA {html.escape(MARQUE)}</title>
<style>:root{{--l:#0B5D6B;--v:#17B6C4;--c:#E8542F;--s:#FFF6E8;--n:#1D2A2E}}
body{{margin:0;font-family:system-ui,sans-serif;background:var(--s);color:var(--n)}}
header{{background:var(--l);color:#fff;padding:28px 16px}}h1{{margin:0 0 6px}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px;padding:16px;max-width:1100px;margin:auto}}
.c{{background:#fff;border-radius:12px;padding:14px;border-top:4px solid var(--v)}}.e{{font-size:28px}}
small{{display:block;color:var(--c);text-transform:uppercase;font-size:11px;letter-spacing:.06em;margin:2px 0 6px}}
p{{margin:0;font-size:14px;line-height:1.4}}</style></head><body>
<header><h1>Bureau IA {html.escape(MARQUE)}</h1>{len(REGISTRE.agents)} agents · {html.escape(VITRINE_PIED)}</header>
<main>{cartes}</main></body></html>"""


# =====================================================================
#  MCP — validation par Will (routes ajoutées SEULEMENT si MCP est actif : sinon elles n'existent pas)
# =====================================================================
@app.get("/admin/mcp/attente")
async def attente_mcp(request: Request):
    if not autorise(request, ADMIN_KEY):
        return erreur(401, "Clé admin invalide", "auth")
    if MCPC is None:
        return erreur(404, "MCP n'est pas activé", "introuvable")
    return {"actions": await asyncio.to_thread(MCPC.base.actions, None, 100)}


async def _decision_api(request: Request, ident: str, verbe: str):
    if MCPC is not None and MCP_CLE_PB:
        return erreur(503, MCP_CLE_PB, "config")                      # refus de fonctionner avec une clé admin inadaptée (avant même de comparer la clé fournie)
    if not autorise(request, ADMIN_KEY):
        return erreur(401, "Clé admin invalide", "auth")
    if MCPC is None:
        return erreur(404, "MCP n'est pas activé", "introuvable")
    if not re.fullmatch(r"[0-9a-f]{10}", ident):
        return erreur(404, "Action introuvable", "introuvable")
    res = await getattr(MCPC, verbe)(ident, "admin (API)")
    return JSONResponse(status_code=200 if res["ok"] else (404 if res["code"] == "introuvable" else 409), content=res)


@app.post("/admin/mcp/valider/{ident}")
async def valider_mcp(ident: str, request: Request):
    return await _decision_api(request, ident, "valider")


@app.post("/admin/mcp/refuser/{ident}")
async def refuser_mcp(ident: str, request: Request):
    return await _decision_api(request, ident, "refuser")


@app.post("/admin/mcp/classer/{ident}")
async def classer_mcp(ident: str, request: Request):
    return await _decision_api(request, ident, "classer")


MCP_COOKIE = "pk_mcp"


def _prefixe_mcp() -> str:
    return MCPM.prefixe_de(cfg("PK_MCP_URL_PUBLIQUE", "").strip())


def _html_mcp(corps: str, statut: int = 200) -> HTMLResponse:
    return HTMLResponse(corps, status_code=statut, headers=MCPM.EN_TETES_PAGE)


def _retour(chemin: str, code: str = "", cookie: str | None = None, efface: bool = False, request: Request | None = None) -> Response:
    """303 vers une adresse RELATIVE (la page marche derrière n'importe quel préfixe de chemin du serveur web)."""
    r = Response(status_code=303, headers={**MCPM.EN_TETES_PAGE, "Location": chemin + (f"?m={code}" if code else "")})
    secure = request is not None and (request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https")
    if cookie:
        r.set_cookie(MCP_COOKIE, cookie, max_age=MCPM.DUREE_SESSION, path=_prefixe_mcp(), httponly=True, samesite="strict", secure=secure)
    if efface:
        r.delete_cookie(MCP_COOKIE, path=_prefixe_mcp())
    return r


def _session_mcp(request: Request) -> str | None:
    v = request.cookies.get(MCP_COOKIE)
    return v if MCPM.session_valide(ADMIN_KEY, v) else None


async def _formulaire(request: Request) -> dict | None:
    from urllib.parse import parse_qs
    corps = await request.body()
    if len(corps) > 4096:
        return None
    origine = request.headers.get("origin")
    if origine and origine != "null":
        hote = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        if (origine.split("://", 1)[-1].rstrip("/")) != hote:
            return None
    elif origine == "null":
        return None
    try:
        return {k: v[0] for k, v in parse_qs(corps.decode("utf-8"), keep_blank_values=True).items()}
    except UnicodeDecodeError:
        return None


def _poser_routes_mcp() -> None:
    try:
        charger_module_mcp()
    except Exception as e:  # noqa: BLE001
        LOG.error("Routes MCP non posées (%s)", type(e).__name__)
        return
    global MCP_CLE_PB
    P = _prefixe_mcp()
    limiteur = MCPM.Limiteur()
    MCP_CLE_PB = MCPM.verifier_cle_admin(cfg("PK_ADMIN_KEY"), LITELLM_KEY, cfg("PK_API_KEY"))
    if MCP_CLE_PB:
        LOG.error("MCP : %s La page %s/ et les routes /admin/mcp/valider|refuser|classer refusent de répondre.", MCP_CLE_PB, P)

    async def racine(request: Request):
        if MCPC is None:
            return erreur(404, "MCP n'est pas activé", "introuvable")
        if MCP_CLE_PB:
            return erreur(503, MCP_CLE_PB, "config")
        session = _session_mcp(request)
        code = request.query_params.get("m", "")
        code = code if code in MCPM.MESSAGES else ""
        if not session:
            return _html_mcp(MCPM.page_connexion(code))
        lignes = await asyncio.to_thread(MCPC.base.actions, None, 200)
        return _html_mcp(MCPM.page_validation(lignes, ADMIN_KEY, session, code))

    async def sans_barre(request: Request):
        return Response(status_code=307, headers={"Location": P.rsplit("/", 1)[-1] + "/"})

    async def connexion(request: Request):
        if MCPC is None:
            return erreur(404, "MCP n'est pas activé", "introuvable")
        if MCP_CLE_PB:
            return erreur(503, MCP_CLE_PB, "config")
        champs = await _formulaire(request)
        if champs is None:
            return _html_mcp(MCPM.page_connexion("invalide"), 400)
        if await limiteur.verifier(champs.get("cle", ""), ADMIN_KEY):
            return _retour("./", "connecte", cookie=MCPM.nouvelle_session(ADMIN_KEY), request=request)
        return _html_mcp(MCPM.page_connexion("refus_cle"), 401)

    async def deconnexion(request: Request):
        return _retour("./", efface=True, request=request)

    def decision(verbe: str):
        async def f(ident: str, request: Request):
            if MCPC is None:
                return erreur(404, "MCP n'est pas activé", "introuvable")
            if MCP_CLE_PB:
                return erreur(503, MCP_CLE_PB, "config")
            session = _session_mcp(request)
            champs = await _formulaire(request)
            if not session or champs is None or not re.fullmatch(r"[0-9a-f]{10}", ident) or not MCPM.csrf_valide(ADMIN_KEY, session, ident, verbe, champs.get("jeton", "")):
                return _retour("../", "invalide", request=request)
            res = await getattr(MCPC, {"valider": "valider", "refuser": "refuser", "classer": "classer"}[verbe])(ident, "Will (page web)")
            return _retour("../", res["code"], request=request)
        return f

    ajouter = app.add_api_route
    ajouter(P, sans_barre, methods=["GET"], include_in_schema=False)
    ajouter(P + "/", racine, methods=["GET"], include_in_schema=False)
    ajouter(P + "/connexion", connexion, methods=["POST"], include_in_schema=False)
    ajouter(P + "/deconnexion", deconnexion, methods=["POST"], include_in_schema=False)
    for v in ("valider", "refuser", "classer"):
        ajouter(P + f"/{v}/{{ident}}", decision(v), methods=["POST"], include_in_schema=False)


if MCP_ACTIF:
    _poser_routes_mcp()


# =====================================================================
#  Serveur MCP du Bureau IA (lecture seule) — route POST /mcp ajoutée SEULEMENT si PK_MCP_SERVEUR_ACTIF=1
# =====================================================================
async def _poser_a_un_agent(agent_id: str, question: str) -> str:
    """Question à un agent SANS AUCUN OUTIL (ni NAS, ni MCP) : appeler() direct, jamais appeler_agent(). Le contenu n'est pas journalisé."""
    REGISTRE.recharger()
    a = REGISTRE.agents.get(agent_id)
    if a is None:
        raise RuntimeError("agent absent du registre")
    corps, chaine = preparer_corps({"messages": [{"role": "user", "content": question}], "max_tokens": 1200}, {**a, "outils": []})
    debut = time.monotonic()
    CTX_DEMANDE.set({"id": "", "demandeur": "mcp", "sujet": "", "fournisseur": ""})
    try:
        rep, cerveau = await appeler(app.state.client, corps, chaine)
    except EchecCerveaux:
        await JOURNAL.ecrire(agent_id, "mcp-serveur", "-", debut, False, "cerveaux indisponibles")
        raise
    texte = MCPS.nettoyer_reponse(masquer(texte_de_la_reponse(rep)))
    await JOURNAL.ecrire(agent_id, "mcp-serveur", cerveau, debut, bool(texte), "")
    return texte or "(réponse vide)"


def _agents_exposes(ids: tuple) -> list[dict]:
    REGISTRE.recharger()
    return [{"id": a["id"], "nom": a["nom"], "famille": a["famille"], "description": a["description"]}
            for i in ids if (a := REGISTRE.agents.get(i))]


def _poser_route_mcp_serveur() -> None:
    global MCPS, MCPSRV, MCP_SERVEUR_PB
    try:
        spec = importlib.util.spec_from_file_location("pk_mcp_serveur", Path(__file__).with_name("mcp_serveur.py"))
        MCPS = importlib.util.module_from_spec(spec)
        sys.modules["pk_mcp_serveur"] = MCPS
        spec.loader.exec_module(MCPS)
        config = MCPS.charger_config(Path(cfg("PK_MCP_SERVEUR_CONFIG") or (AGENTS_DIR / "mcp_serveur.yaml")), set(REGISTRE.agents))
        MCPSRV = MCPS.Serveur(config, _agents_exposes, _poser_a_un_agent)
    except Exception as e:  # noqa: BLE001
        LOG.error("Serveur MCP NON démarré (%s : %s)", type(e).__name__, masquer(str(e))[:200])
        return
    for e in config.erreurs:
        LOG.warning("mcp_serveur.yaml : %s", e)
    MCP_SERVEUR_PB = MCPS.verifier_cle(cfg("PK_MCP_SERVEUR_CLE"), {"LITELLM_MASTER_KEY": LITELLM_KEY, "PK_API_KEY": cfg("PK_API_KEY"),
                                                                  "PK_ADMIN_KEY": cfg("PK_ADMIN_KEY")})
    if MCP_SERVEUR_PB:
        LOG.error("Serveur MCP : %s La route /mcp refuse de répondre.", MCP_SERVEUR_PB)
    else:
        LOG.info("Serveur MCP actif (lecture seule) : %d agent(s) exposé(s)%s", len(config.agents), "" if config.agents else " — AUCUN (agents/mcp_serveur.yaml absent ou vide)")

    async def mcp(request: Request):
        if MCP_SERVEUR_PB:
            return erreur(503, "Serveur MCP indisponible (configuration) : voir /health et les journaux", "config")
        if not autorise(request, cfg("PK_MCP_SERVEUR_CLE")):
            return JSONResponse(status_code=401, content={"error": {"message": "Clé du serveur MCP invalide", "type": "auth"}}, headers={"WWW-Authenticate": "Bearer"})
        origine = request.headers.get("origin")
        if origine:
            hote = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
            if origine.split("://", 1)[-1].rstrip("/") != hote and origine.rstrip("/") not in MCPSRV.cfg.origines:
                return erreur(403, "Origine refusée", "auth")
        taille = request.headers.get("content-length", "")
        if taille.isdigit() and int(taille) > MCPS.CORPS_MAX:
            return erreur(413, "Requête trop volumineuse", "requete")
        statut, corps = await MCPSRV.traiter(await request.body())
        if corps is None:
            return Response(status_code=statut)
        return JSONResponse(status_code=statut, content=corps)

    async def pas_permis(request: Request):
        return JSONResponse(status_code=405, content={"error": {"message": "POST seulement (serveur sans session ni flux)", "type": "requete"}}, headers={"Allow": "POST"})

    app.add_api_route("/mcp", mcp, methods=["POST"], include_in_schema=False)
    app.add_api_route("/mcp", pas_permis, methods=["GET", "DELETE", "PUT"], include_in_schema=False)


if MCP_SERVEUR_ACTIF:
    _poser_route_mcp_serveur()
