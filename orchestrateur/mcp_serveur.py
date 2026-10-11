"""Serveur MCP du Bureau IA : expose des agents EN LECTURE SEULE à un client MCP (Claude Desktop, un autre outil…).

DÉSACTIVÉ PAR DÉFAUT (PK_MCP_SERVEUR_ACTIF=0). Jamais pour une autre marque que PixelKréol (ABI : coupé). Indépendant du client MCP (mcp_client.py).

Garanties (vérifiées par des tests) :
  * CLÉ DÉDIÉE (PK_MCP_SERVEUR_CLE) : 32 caractères au moins, différente de LITELLM_MASTER_KEY, PK_API_KEY et PK_ADMIN_KEY ; sinon le serveur refuse de répondre
    (503 avec la raison, visible dans /health et les journaux). La clé API et la clé admin ne donnent PAS accès à ce serveur, et inversement.
  * LECTURE SEULE : deux outils seulement.
      - lister_agents : id, nom, famille, description des agents autorisés (jamais leurs fiches de poste) ;
      - poser_question : une question à un agent autorisé, SANS aucun outil (ni NAS, ni MCP, ni écriture) : l'agent répond avec son seul savoir de modèle.
    Aucune action d'écriture, d'envoi ou de paiement n'est joignable par ce serveur.
  * LISTE BLANCHE D'AGENTS dans agents/mcp_serveur.yaml (absente = aucun agent exposé).
  * BORNES : taille de la question et de la réponse, cadence par minute, appels simultanés, délai ; corps de requête limité.
  * Contrôle de l'en-tête Origin (protection contre les attaques par navigateur) ; réponses sans état (pas de session).
  * JOURNAL : agent, durée, succès ; jamais le contenu de la question ni de la réponse.
⚠ La question posée part vers le fournisseur de modèle en ligne de l'agent (comme tout message au Bureau IA) : pas de données de clients ni d'ABI.
Dépendances : bibliothèque standard + pyyaml.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

LOG = logging.getLogger("pk.mcp_serveur")

CLE_MIN = 32
VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
CORPS_MAX = 64 * 1024
NOM = "pixelkreol-bureau-ia"


def verifier_cle(cle: str, autres: dict[str, str]) -> str | None:
    """None si la clé du serveur convient, sinon la raison (jamais la clé). `autres` : nom → valeur des autres clés du Bureau IA."""
    if not cle:
        return "PK_MCP_SERVEUR_CLE absente : le serveur MCP est désactivé. Définir dans .env une clé dédiée (32 caractères au moins)."
    if len(cle) < CLE_MIN:
        return f"PK_MCP_SERVEUR_CLE fait {len(cle)} caractères (minimum {CLE_MIN}) : le serveur MCP est désactivé."
    for nom, valeur in autres.items():
        if valeur and hmac.compare_digest(cle.encode(), valeur.encode()):
            return f"PK_MCP_SERVEUR_CLE est identique à {nom} : le serveur MCP est désactivé. Utiliser une clé distincte."
    return None


@dataclass
class Config:
    agents: tuple = ()
    question_max: int = 4000
    reponse_max: int = 8000
    appels_par_minute: int = 20
    simultanes: int = 2
    delai: float = 90.0
    origines: tuple = ()
    erreurs: list = field(default_factory=list)


def _entier(v: Any, defaut: int, mini: int, maxi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return defaut
    return max(mini, min(maxi, n))


def charger_config(chemin: Path, ids_agents: set[str] | None = None) -> Config:
    cfg = Config()
    chemin = Path(chemin)
    if not chemin.is_file():
        return cfg
    try:
        brut = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        cfg.erreurs.append(f"mcp_serveur.yaml illisible : {str(e)[:150]} (aucun agent exposé)")
        return cfg
    if not isinstance(brut, dict):
        cfg.erreurs.append("mcp_serveur.yaml : un objet est attendu (aucun agent exposé)")
        return cfg
    agents = brut.get("agents") or []
    if not isinstance(agents, list) or not all(isinstance(a, str) and a for a in agents):
        cfg.erreurs.append("mcp_serveur.yaml : « agents » doit être une liste d'identifiants (aucun agent exposé)")
        agents = []
    if ids_agents is not None:
        inconnus = [a for a in agents if a not in ids_agents]
        if inconnus:
            cfg.erreurs.append(f"agent(s) inconnu(s) ignoré(s) : {', '.join(inconnus)}")
        agents = [a for a in agents if a in ids_agents]
    cfg.agents = tuple(dict.fromkeys(agents))
    lim = brut.get("limites") if isinstance(brut.get("limites"), dict) else {}
    cfg.question_max = _entier(lim.get("question_max"), 4000, 100, 20000)
    cfg.reponse_max = _entier(lim.get("reponse_max"), 8000, 200, 50000)
    cfg.appels_par_minute = _entier(lim.get("appels_par_minute"), 20, 1, 300)
    cfg.simultanes = _entier(lim.get("simultanes"), 2, 1, 4)
    cfg.delai = float(_entier(lim.get("delai"), 90, 5, 300))
    origines = brut.get("origines") or []
    cfg.origines = tuple(o.rstrip("/") for o in origines if isinstance(o, str) and o.startswith(("https://", "http://localhost", "http://127.0.0.1")))
    return cfg


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
_APPELS = re.compile(r"<\s*(tool_call|function_call|tool_use|tool_response)\b[^>]*>.*?(<\s*/\s*\1\s*>|\Z)|<\s*/?\s*(function|parameter)\b[^>]*>?", re.S | re.I)


def nettoyer_reponse(texte: Any) -> str:
    """Texte d'agent renvoyé au client MCP : sans caractères de contrôle ni balises d'appel d'outil."""
    return _APPELS.sub(" ", _CTRL.sub("", texte if isinstance(texte, str) else "")).strip()


def _outils(cfg: Config) -> list[dict]:
    return [
        {"name": "lister_agents", "title": "Lister les agents du Bureau IA",
         "description": "Liste les agents du Bureau IA PixelKréol que ce serveur expose (identifiant, nom, famille, rôle).",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
         "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "poser_question", "title": "Poser une question à un agent",
         "description": ("Pose une question à un agent du Bureau IA et renvoie sa réponse. L'agent répond SANS outils (aucun accès aux fichiers, "
                         "aux mails ni aux agendas) et n'agit jamais : il rédige, explique, propose. Ne pas y mettre de données de clients."),
         "inputSchema": {"type": "object", "properties": {
             "agent": {"type": "string", "enum": list(cfg.agents), "description": "identifiant de l'agent (voir lister_agents)"},
             "question": {"type": "string", "maxLength": cfg.question_max, "description": "la question"}},
             "required": ["agent", "question"], "additionalProperties": False},
         "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}},
    ]


class Serveur:
    """Traite les requêtes JSON-RPC. `lister()` renvoie les agents exposés ; `poser(agent_id, question)` renvoie le texte de la réponse (ou lève)."""

    def __init__(self, cfg: Config, lister: Callable[[tuple], list[dict]], poser: Callable[[str, str], Awaitable[str]]):
        self.cfg, self._lister, self._poser = cfg, lister, poser
        self.cadence: deque = deque()
        self.sem = asyncio.Semaphore(cfg.simultanes)

    # --- JSON-RPC ---------------------------------------------------------------------
    @staticmethod
    def _erreur(rid: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    def _cadence_ok(self) -> bool:
        t = time.monotonic()
        while self.cadence and t - self.cadence[0] > 60:
            self.cadence.popleft()
        if len(self.cadence) >= self.cfg.appels_par_minute:
            return False
        self.cadence.append(t)
        return True

    async def traiter(self, octets: bytes) -> tuple[int, dict | None]:
        """(statut HTTP, corps JSON ou None)."""
        if len(octets) > CORPS_MAX:
            return 413, self._erreur(None, -32600, "requête trop volumineuse")
        try:
            msg = json.loads(octets.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return 400, self._erreur(None, -32700, "JSON illisible")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return 400, self._erreur(None, -32600, "requête JSON-RPC 2.0 unique attendue (les lots ne sont pas acceptés)")
        rid, methode, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if not isinstance(methode, str) or not isinstance(params, dict):
            return 400, self._erreur(rid, -32600, "requête invalide")
        if "id" not in msg:                                             # notification : pas de réponse
            return 202, None
        if methode == "initialize":
            demande = params.get("protocolVersion")
            version = demande if demande in VERSIONS else VERSIONS[0]
            return 200, {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": NOM, "version": "1"},
                "instructions": "Serveur en lecture seule : lister_agents et poser_question. Les agents répondent sans outils."}}
        if methode == "ping":
            return 200, {"jsonrpc": "2.0", "id": rid, "result": {}}
        if methode == "tools/list":
            return 200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": _outils(self.cfg)}}
        if methode == "tools/call":
            return 200, {"jsonrpc": "2.0", "id": rid, "result": await self._appel_outil(params)}
        return 200, self._erreur(rid, -32601, "méthode inconnue (ce serveur n'expose que des outils en lecture seule)")

    @staticmethod
    def _texte(texte: str, erreur: bool = False) -> dict:
        return {"content": [{"type": "text", "text": texte}], "isError": erreur}

    async def _appel_outil(self, params: dict) -> dict:
        nom, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(args, dict):
            return self._texte("Arguments invalides.", True)
        if nom == "lister_agents":
            return self._texte(json.dumps(self._lister(self.cfg.agents), ensure_ascii=False, indent=2))
        if nom != "poser_question":
            return self._texte("Outil inconnu.", True)
        agent, question = args.get("agent"), args.get("question")
        if not isinstance(agent, str) or agent not in self.cfg.agents:
            return self._texte("Agent inconnu ou non exposé par ce serveur (voir lister_agents).", True)
        if not isinstance(question, str) or not question.strip():
            return self._texte("La question est vide.", True)
        if len(question) > self.cfg.question_max:
            return self._texte(f"Question trop longue (maximum {self.cfg.question_max} caractères).", True)
        if not self._cadence_ok():
            return self._texte("Trop de questions à la minute : réessayez dans un instant.", True)
        async with self.sem:
            try:
                texte = await asyncio.wait_for(self._poser(agent, question), self.cfg.delai)
            except asyncio.TimeoutError:
                return self._texte("L'agent n'a pas répondu à temps.", True)
            except Exception as e:  # noqa: BLE001 — jamais de détail technique vers le client
                LOG.warning("poser_question(%s) en échec : %s", agent, type(e).__name__)
                return self._texte("L'agent est indisponible pour le moment.", True)
        if len(texte) > self.cfg.reponse_max:
            texte = texte[:self.cfg.reponse_max].rstrip() + " […tronqué]"
        return self._texte(texte)
