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
import datetime as dt
import hmac
import html
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

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
LIEU = cfg("PK_LIEU", "à La Réunion")                         # « Date et heure <lieu> : … »
FUSEAU = cfg("PK_FUSEAU", "Indian/Reunion")                   # fuseau horaire donné aux agents
PATRON = cfg("PK_PATRON", "Will")                             # « Demande de <patron> : … » (synthèse d'équipe)
VITRINE_PIED = cfg("PK_VITRINE_PIED", "LLM gratuits · La Réunion")
PROPRIETAIRE = cfg("PK_PROPRIETAIRE", "pixelkreol")           # champ owned_by du catalogue /v1/models
try:
    TZ = ZoneInfo(FUSEAU)
except (ZoneInfoNotFoundError, ValueError):
    LOG.warning("PK_FUSEAU « %s » inconnu : fuseau Indian/Reunion utilisé", FUSEAU)
    TZ = ZoneInfo("Indian/Reunion")


# =====================================================================
#  Outils texte
# =====================================================================
def normaliser(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte.lower())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    texte = re.sub(r"[^a-z0-9' -]+", " ", texte.replace("’", "'"))
    return re.sub(r"\s+", " ", texte)


_SECRETS = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{20,}"
                      r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{6,})")


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


def contient_image(messages: list[dict]) -> bool:
    for m in messages:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") in ("image_url", "input_image") for p in c):
            return True
    return False


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
        return None

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

    def prompt_systeme(self, a: dict) -> str:
        maintenant = dt.datetime.now(TZ)
        date = f"{JOURS[maintenant.weekday()]} {maintenant.strftime('%d/%m/%Y %H:%M')}"
        parties = [self.socle,
                   f"## Ton poste : {a['emoji']} {a['nom']}\n{a['description']}\n\n{a['consignes'].strip()}",
                   f"Date et heure {LIEU} : {date}."]
        if a["id"] == "orchestrateur":
            equipe = "\n".join(f"- {x['emoji']} {x['nom']} ({x['id']}) : {x['description']}"
                               for x in self.agents.values() if x["id"] != "orchestrateur")
            parties.append("## L'équipe que tu coordonnes\n" + equipe)
        return "\n\n".join(p for p in parties if p)


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


def preparer_corps(corps: dict, agent: dict) -> tuple[dict, list[str]]:
    messages = [m for m in corps.get("messages", []) if isinstance(m, dict)]
    systeme_client = "\n\n".join(texte_du_message(m) for m in messages if m.get("role") == "system").strip()
    systeme = REGISTRE.prompt_systeme(agent)
    if systeme_client:
        systeme += "\n\n## Consignes supplémentaires de l'utilisateur\n" + systeme_client
    nouveaux = [{"role": "system", "content": systeme}] + [m for m in messages if m.get("role") != "system"]
    sortie = {k: v for k, v in corps.items() if k not in ("messages", "model") and option_autorisee(k)}
    sortie["messages"] = nouveaux
    sortie.setdefault("temperature", agent.get("temperature", 0.4))
    cerveau = "pk-vision" if contient_image(messages) else agent["cerveau"]
    chaine = [cerveau] + [c for c in REGISTRE.secours(cerveau) if c != cerveau]
    if cerveau == "pk-vision":  # une image ne doit pas partir vers un cerveau texte seul
        chaine = ["pk-vision"]
    return sortie, chaine


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


async def appeler(client: httpx.AsyncClient, corps: dict, chaine: list[str]) -> tuple[dict, str]:
    dernier = (502, "aucun cerveau essayé")
    essayes: list[str] = []
    for cerveau in chaine:
        essayes.append(cerveau)
        try:
            r = await client.post(f"{LITELLM_URL}/v1/chat/completions", json={**corps, "model": cerveau, "stream": False},
                                  headers=entetes_litellm(), timeout=TIMEOUT)
        except httpx.HTTPError as e:
            delai = isinstance(e, httpx.TimeoutException)
            dernier = (504 if delai else 503, f"{cerveau} {'délai dépassé' if delai else 'injoignable'} ({type(e).__name__})")
            LOG.warning("bascule : %s", dernier[1])
            continue
        if r.status_code == 200:
            try:
                rep = r.json()
            except ValueError:
                dernier = (502, f"{cerveau} : réponse illisible (pas du JSON)")
                LOG.warning("bascule : %s", dernier[1])
                continue
            if reponse_vide(rep):
                dernier = (502, f"{cerveau} : réponse vide")
                LOG.warning("bascule : %s", dernier[1])
                continue
            return rep, cerveau
        dernier = (r.status_code, masquer(f"{cerveau} : {r.text[:300]}"))
        if not statut_rebascule(r.status_code):
            break
        LOG.warning("bascule : %s a répondu %s", cerveau, r.status_code)
    raise EchecCerveaux(*dernier, essayes=essayes)


async def ouvrir_flux(client: httpx.AsyncClient, corps: dict, chaine: list[str]):
    """Ouvre le flux du premier cerveau qui démarre vraiment. Renvoie (réponse, cerveau, 1er bloc, suite).

    On lit le premier bloc AVANT de s'engager : un flux vide ou qui commence par une erreur permet encore
    de basculer sur le cerveau suivant. Après le premier octet transmis, on ne rebascule plus
    (sinon la réponse serait facturée deux fois)."""
    dernier = (502, "aucun cerveau essayé")
    essayes: list[str] = []
    for cerveau in chaine:
        essayes.append(cerveau)
        req = client.build_request("POST", f"{LITELLM_URL}/v1/chat/completions",
                                   json={**corps, "model": cerveau, "stream": True},
                                   headers=entetes_litellm(), timeout=httpx.Timeout(TIMEOUT, connect=10))
        try:
            r = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            delai = isinstance(e, httpx.TimeoutException)
            dernier = (504 if delai else 503, f"{cerveau} {'délai dépassé' if delai else 'injoignable'} ({type(e).__name__})")
            LOG.warning("bascule (flux) : %s", dernier[1])
            continue
        if r.status_code != 200:
            texte = (await r.aread()).decode("utf-8", "replace")[:300]
            await r.aclose()
            dernier = (r.status_code, masquer(f"{cerveau} : {texte}"))
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
            continue
        if not premier.strip():
            await r.aclose()
            dernier = (502, f"{cerveau} : flux vide")
            LOG.warning("bascule (flux) : %s", dernier[1])
            continue
        err = erreur_du_bloc(premier)
        if err:
            await r.aclose()
            dernier = (err[0], masquer(f"{cerveau} : {err[1]}"))
            if not statut_rebascule(err[0]):
                break
            LOG.warning("bascule (flux) : %s a signalé l'erreur %s", cerveau, err[0])
            continue
        return r, cerveau, premier, suite
    raise EchecCerveaux(*dernier, essayes=essayes)


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
class Journal:
    def __init__(self) -> None:
        self.pool = None
        self.compteurs: dict[str, dict[str, int]] = {}
        self.echecs_ecriture = 0

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
            LOG.info("Journal Postgres actif")
        except Exception as e:  # noqa: BLE001
            LOG.warning("Journal Postgres indisponible (%s) — journal en mémoire seulement", masquer(str(e))[:200])
            self.pool = None

    async def ecrire(self, agent: str, mode: str, cerveau: str, debut: float, ok: bool, detail: str = "") -> None:
        c = self.compteurs.setdefault(agent, {"ok": 0, "echec": 0})
        c["ok" if ok else "echec"] += 1
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as con:
                await con.execute("INSERT INTO pk_journal(agent,mode,cerveau,duree_ms,ok,detail) VALUES($1,$2,$3,$4,$5,$6)",
                                  agent, mode, cerveau, int((time.monotonic() - debut) * 1000), ok, masquer(detail)[:500])
            self.echecs_ecriture = 0
        except Exception as e:  # noqa: BLE001 — le journal ne doit jamais bloquer une réponse
            self.echecs_ecriture += 1
            if self.echecs_ecriture == 1 or self.echecs_ecriture % 100 == 0:   # pas une ligne par requête
                LOG.warning("journal Postgres : écriture impossible (%s) — %d échec(s) de suite",
                            masquer(str(e))[:200], self.echecs_ecriture)

    async def arreter(self) -> None:
        if self.pool is not None:
            await self.pool.close()


JOURNAL = Journal()
ETAT = {"litellm": None}   # dernier état connu de LiteLLM, pour ne journaliser que les changements


# =====================================================================
#  Application
# =====================================================================
@asynccontextmanager
async def cycle_de_vie(app: FastAPI):
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, connect=10))
    if not API_KEY:
        LOG.error("Aucune clé (PK_API_KEY / LITELLM_MASTER_KEY) : l'API reste FERMÉE tant qu'elle n'est pas définie")
    await JOURNAL.demarrer()
    yield
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
    return {"ok": bool(REGISTRE.agents), "version": VERSION, "agents": len(REGISTRE.agents),
            "litellm": litellm_ok, "avertissements": REGISTRE.avertissements}


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
        return await mode_equipe(client, corps, flux)

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

    if not flux:
        try:
            reponse, cerveau = await appeler(client, corps_final, chaine)
        except EchecCerveaux as e:
            await JOURNAL.ecrire(agent["id"], mode, "-", debut, False, e.detail)
            return erreur(e.statut if e.statut >= 400 else 502, e.message(agent["nom"]), "cerveaux_indisponibles")
        reponse["model"] = agent["modele"]
        if annonce and reponse.get("choices"):
            msg = reponse["choices"][0].setdefault("message", {})
            msg["content"] = annonce + (msg.get("content") or "")
        await JOURNAL.ecrire(agent["id"], mode, cerveau, debut, True)
        return JSONResponse(reponse, headers={"X-PK-Agent": agent["id"], "X-PK-Cerveau": cerveau})

    try:
        amont, cerveau, premier, suite = await ouvrir_flux(client, corps_final, chaine)
    except EchecCerveaux as e:
        await JOURNAL.ecrire(agent["id"], mode, "-", debut, False, e.detail)
        return erreur(e.statut if e.statut >= 400 else 502, e.message(agent["nom"]), "cerveaux_indisponibles")

    async def relais() -> AsyncIterator[bytes]:
        ok = True
        detail = ""
        try:
            if annonce:
                yield morceau_sse(agent["modele"], annonce)
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
                         usage: dict | None = None) -> tuple[str, bool]:
    """Fait travailler un agent. Ne lève jamais d'exception : renvoie (texte, réussi)."""
    corps, chaine = preparer_corps({"messages": messages, "max_tokens": 1200}, agent)
    debut = time.monotonic()
    try:
        rep, cerveau = await asyncio.wait_for(appeler(client, corps, chaine), DELAI_AGENT)
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


async def mode_equipe(client: httpx.AsyncClient, corps: dict, flux: bool):
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
    plan_txt, ok = await executer_agent(client, directeur, [{"role": "user", "content": consigne_plan}], usage)
    plan = extraire_json(plan_txt) if ok else None
    taches = choisir_taches(plan, demande, ids)
    notes: list[str] = []
    if plan is None:
        notes.append("Le découpage automatique n'a pas fonctionné : agents choisis d'après les mots-clés de ta demande.")
        LOG.warning("pk-equipe : plan du directeur inutilisable (%s)", "directeur en panne" if not ok else "JSON illisible")

    historique = messages[:-1][-6:]
    resultats = await asyncio.gather(*[
        executer_agent(client, a, historique + [{"role": "user", "content": f"{c}\n\n(Demande d'origine : {demande})"}], usage)
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
    synthese_txt, ok_s = await executer_agent(client, directeur, [{"role": "user", "content": prompt_synthese}], usage)

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
