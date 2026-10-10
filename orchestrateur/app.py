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
from zoneinfo import ZoneInfo

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
TZ = ZoneInfo("Indian/Reunion")


def cfg(nom: str, defaut: str = "") -> str:
    return os.getenv(nom, defaut)


LITELLM_URL = cfg("LITELLM_URL", "http://pk_litellm:4000").rstrip("/")
LITELLM_KEY = cfg("LITELLM_MASTER_KEY", "")
API_KEY = cfg("PK_API_KEY") or LITELLM_KEY
ADMIN_KEY = cfg("PK_ADMIN_KEY") or API_KEY
AGENTS_DIR = Path(cfg("PK_AGENTS_DIR", str(Path(__file__).resolve().parent.parent / "agents")))
TIMEOUT = float(cfg("PK_TIMEOUT", "120"))
ROUTAGE_IA = cfg("PK_ROUTAGE_IA", "true").lower() == "true"
ANNONCE = cfg("PK_ANNONCE_AGENT", "true").lower() == "true"
MAX_EQUIPE = int(cfg("PK_EQUIPE_MAX", "4"))
DATABASE_URL = cfg("DATABASE_URL", "")


# =====================================================================
#  Outils texte
# =====================================================================
def normaliser(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte.lower())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9' -]+", " ", texte.replace("’", "'"))


def texte_du_message(message: dict) -> str:
    contenu = message.get("content")
    if isinstance(contenu, str):
        return contenu
    if isinstance(contenu, list):
        return " ".join(p.get("text", "") for p in contenu if isinstance(p, dict) and p.get("type") == "text")
    return ""


def contient_image(messages: list[dict]) -> bool:
    for m in messages:
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") in ("image_url", "input_image") for p in c):
            return True
    return False


def dernier_message_utilisateur(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return texte_du_message(m)
    return ""


def erreur(statut: int, message: str, code: str = "erreur") -> JSONResponse:
    return JSONResponse(status_code=statut, content={"error": {"message": message, "type": code, "code": statut}})


# =====================================================================
#  Registre des agents (rechargé à chaud)
# =====================================================================
class Registre:
    def __init__(self, dossier: Path):
        self.dossier = dossier
        self._empreinte: tuple = ()
        self.agents: dict[str, dict] = {}
        self.socle = ""
        self.cerveaux: dict = {}
        self.avertissements: list[str] = []
        self.recharger(force=True)

    def _fichiers(self) -> list[Path]:
        return [self.dossier / n for n in ("registre.yaml", "perso.yaml", "socle.txt", "cerveaux.yaml")]

    def _calcul_empreinte(self) -> tuple:
        return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else (str(p), 0, 0)
                     for p in self._fichiers())

    @staticmethod
    def valider(a: dict, ids: set[str], nums: set[int]) -> str | None:
        for champ in ("id", "num", "nom", "cerveau", "description", "consignes"):
            if not a.get(champ) and a.get(champ) != 0:
                return f"champ « {champ} » manquant"
        if not ID_RE.match(str(a["id"])):
            return f"id « {a['id']} » invalide (minuscules, chiffres, tirets)"
        if a["id"] in ("auto", "equipe"):
            return f"id « {a['id']} » réservé"
        if a["id"] in ids:
            return f"id « {a['id']} » déjà utilisé"
        try:
            num = int(a["num"])
        except (TypeError, ValueError):
            return "num doit être un entier"
        if not 1 <= num <= 999:
            return "num doit être entre 1 et 999"
        if num in nums:
            return f"num {num} déjà utilisé"
        if a["cerveau"] not in CERVEAUX_VALIDES:
            return f"cerveau « {a['cerveau']} » inconnu ({', '.join(sorted(CERVEAUX_VALIDES))})"
        if a.get("famille") and a["famille"] not in FAMILLES:
            return f"famille « {a['famille']} » inconnue"
        t = a.get("temperature", 0.4)
        if not isinstance(t, (int, float)) or not 0 <= t <= 1.5:
            return "temperature doit être un nombre entre 0 et 1.5"
        if not isinstance(a.get("mots_cles", []), list):
            return "mots_cles doit être une liste"
        return None

    def recharger(self, force: bool = False) -> None:
        empreinte = self._calcul_empreinte()
        if not force and empreinte == self._empreinte:
            return
        avert: list[str] = []
        agents: dict[str, dict] = {}
        ids: set[str] = set()
        nums: set[int] = set()
        for nom in ("registre.yaml", "perso.yaml"):
            p = self.dossier / nom
            if not p.exists():
                continue
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                avert.append(f"{nom} illisible : {e}")
                if nom == "registre.yaml" and self.agents:
                    LOG.error("registre.yaml invalide, on garde la version précédente : %s", e)
                    self.avertissements = avert
                    return
                continue
            for a in data.get("agents") or []:
                if not isinstance(a, dict):
                    continue
                pb = self.valider(a, ids, nums)
                if pb:
                    avert.append(f"{nom} / {a.get('id', '?')} ignoré : {pb}")
                    continue
                a = dict(a)
                a["num"] = int(a["num"])
                a.setdefault("famille", "client" if nom == "perso.yaml" else "production")
                a.setdefault("emoji", "🤖")
                a.setdefault("temperature", 0.4)
                a.setdefault("mots_cles", [])
                a["source"] = nom
                a["modele"] = f"pk-{a['num']:02d}-{a['id']}"
                a["_mots"] = [normaliser(str(m)).strip() for m in a["mots_cles"] if str(m).strip()]
                agents[a["id"]] = a
                ids.add(a["id"])
                nums.add(a["num"])
        socle_p = self.dossier / "socle.txt"
        cerv_p = self.dossier / "cerveaux.yaml"
        self.socle = socle_p.read_text(encoding="utf-8").strip() if socle_p.exists() else ""
        try:
            self.cerveaux = yaml.safe_load(cerv_p.read_text(encoding="utf-8")) if cerv_p.exists() else {}
        except yaml.YAMLError as e:
            avert.append(f"cerveaux.yaml illisible : {e}")
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
        return list((self.cerveaux or {}).get("secours_entre_cerveaux", {}).get(cerveau, []))

    def prompt_systeme(self, a: dict) -> str:
        maintenant = dt.datetime.now(TZ).strftime("%A %d/%m/%Y %H:%M")
        parties = [self.socle,
                   f"## Ton poste : {a['emoji']} {a['nom']}\n{a['description']}\n\n{a['consignes'].strip()}",
                   f"Date et heure à La Réunion : {maintenant}."]
        if a["id"] == "orchestrateur":
            equipe = "\n".join(f"- {x['emoji']} {x['nom']} ({x['id']}) : {x['description']}"
                               for x in self.agents.values() if x["id"] != "orchestrateur")
            parties.append("## L'équipe que tu coordonnes\n" + equipe)
        return "\n\n".join(p for p in parties if p)


REGISTRE = Registre(AGENTS_DIR)


# =====================================================================
#  Routage automatique
# =====================================================================
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
        score += 1.5
    return score


def classement_mots_cles(texte: str) -> list[tuple[float, dict]]:
    tn = normaliser(texte)
    notes = [(score_agent(a, tn), a) for a in REGISTRE.agents.values()]
    return sorted(notes, key=lambda x: (-x[0], x[1]["num"]))


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
            return None
        reponse = r.json()["choices"][0]["message"]["content"] or ""
        for mot in re.findall(r"[a-z0-9-]+", reponse.lower()):
            if mot in REGISTRE.agents:
                return REGISTRE.agents[mot]
    except Exception as e:  # noqa: BLE001 — le routage IA est un bonus, jamais bloquant
        LOG.info("routage IA indisponible : %s", e)
    return None


async def choisir_agent(client: httpx.AsyncClient, messages: list[dict]) -> tuple[dict, str]:
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
    defaut = REGISTRE.agents.get("orchestrateur") or next(iter(REGISTRE.agents.values()))
    return defaut, "défaut"


# =====================================================================
#  Appels LiteLLM avec bascule entre cerveaux
# =====================================================================
def entetes_litellm() -> dict:
    return {"Authorization": f"Bearer {LITELLM_KEY}", "Content-Type": "application/json"}


def preparer_corps(corps: dict, agent: dict) -> tuple[dict, list[str]]:
    messages = [m for m in corps.get("messages", []) if isinstance(m, dict)]
    systeme_client = "\n\n".join(texte_du_message(m) for m in messages if m.get("role") == "system").strip()
    systeme = REGISTRE.prompt_systeme(agent)
    if systeme_client:
        systeme += "\n\n## Consignes supplémentaires de l'utilisateur\n" + systeme_client
    nouveaux = [{"role": "system", "content": systeme}] + [m for m in messages if m.get("role") != "system"]
    sortie = {k: v for k, v in corps.items() if k not in ("messages", "model")}
    sortie["messages"] = nouveaux
    sortie.setdefault("temperature", agent.get("temperature", 0.4))
    cerveau = "pk-vision" if contient_image(messages) else agent["cerveau"]
    chaine = [cerveau] + [c for c in REGISTRE.secours(cerveau) if c != cerveau]
    if cerveau == "pk-vision":  # une image ne doit pas partir vers un cerveau texte seul
        chaine = ["pk-vision"]
    return sortie, chaine


class EchecCerveaux(Exception):
    def __init__(self, statut: int, detail: str):
        super().__init__(detail)
        self.statut, self.detail = statut, detail


def statut_rebascule(statut: int) -> bool:
    return statut in (404, 408, 429) or statut >= 500


async def appeler(client: httpx.AsyncClient, corps: dict, chaine: list[str]) -> tuple[dict, str]:
    dernier = (502, "aucun cerveau essayé")
    for cerveau in chaine:
        try:
            r = await client.post(f"{LITELLM_URL}/v1/chat/completions", json={**corps, "model": cerveau, "stream": False},
                                  headers=entetes_litellm(), timeout=TIMEOUT)
        except httpx.HTTPError as e:
            dernier = (503, f"{cerveau} injoignable : {e}")
            continue
        if r.status_code == 200:
            return r.json(), cerveau
        dernier = (r.status_code, f"{cerveau} : {r.text[:300]}")
        if not statut_rebascule(r.status_code):
            break
        LOG.warning("bascule : %s a répondu %s", cerveau, r.status_code)
    raise EchecCerveaux(*dernier)


async def ouvrir_flux(client: httpx.AsyncClient, corps: dict, chaine: list[str]) -> tuple[httpx.Response, str]:
    dernier = (502, "aucun cerveau essayé")
    for cerveau in chaine:
        req = client.build_request("POST", f"{LITELLM_URL}/v1/chat/completions",
                                   json={**corps, "model": cerveau, "stream": True},
                                   headers=entetes_litellm(), timeout=httpx.Timeout(TIMEOUT, connect=10))
        try:
            r = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            dernier = (503, f"{cerveau} injoignable : {e}")
            continue
        if r.status_code == 200:
            return r, cerveau
        texte = (await r.aread()).decode("utf-8", "replace")[:300]
        await r.aclose()
        dernier = (r.status_code, f"{cerveau} : {texte}")
        if not statut_rebascule(r.status_code):
            break
        LOG.warning("bascule (flux) : %s a répondu %s", cerveau, r.status_code)
    raise EchecCerveaux(*dernier)


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
            LOG.warning("Journal Postgres indisponible (%s) — journal en mémoire seulement", e)
            self.pool = None

    async def ecrire(self, agent: str, mode: str, cerveau: str, debut: float, ok: bool, detail: str = "") -> None:
        c = self.compteurs.setdefault(agent, {"ok": 0, "echec": 0})
        c["ok" if ok else "echec"] += 1
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as con:
                await con.execute("INSERT INTO pk_journal(agent,mode,cerveau,duree_ms,ok,detail) VALUES($1,$2,$3,$4,$5,$6)",
                                  agent, mode, cerveau, int((time.monotonic() - debut) * 1000), ok, detail[:500])
        except Exception as e:  # noqa: BLE001
            LOG.debug("journal : %s", e)

    async def arreter(self) -> None:
        if self.pool is not None:
            await self.pool.close()


JOURNAL = Journal()


# =====================================================================
#  Application
# =====================================================================
@asynccontextmanager
async def cycle_de_vie(app: FastAPI):
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, connect=10))
    await JOURNAL.demarrer()
    yield
    await app.state.client.aclose()
    await JOURNAL.arreter()


app = FastAPI(title="Bureau IA PixelKréol — Orchestrateur", version=VERSION, lifespan=cycle_de_vie)


def autorise(request: Request, cle: str) -> bool:
    if not cle:
        return True
    auth = request.headers.get("authorization", "")
    fourni = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
    return bool(fourni) and fourni == cle


@app.get("/health")
async def sante(request: Request):
    REGISTRE.recharger()
    litellm_ok = False
    try:
        r = await request.app.state.client.get(f"{LITELLM_URL}/health/liveliness", timeout=5)
        litellm_ok = r.status_code == 200
    except httpx.HTTPError:
        pass
    return {"ok": bool(REGISTRE.agents), "version": VERSION, "agents": len(REGISTRE.agents),
            "litellm": litellm_ok, "avertissements": REGISTRE.avertissements}


@app.get("/v1/models")
async def modeles(request: Request):
    if not autorise(request, API_KEY):
        return erreur(401, "Clé API invalide", "auth")
    REGISTRE.recharger()
    cree = int(time.time())
    donnees = [
        {"id": "pk-auto", "object": "model", "created": cree, "owned_by": "pixelkreol",
         "name": "🧠 PK Auto — choisit le bon agent"},
        {"id": "pk-equipe", "object": "model", "created": cree, "owned_by": "pixelkreol",
         "name": "👥 PK Équipe — plusieurs agents + synthèse"},
    ]
    for a in REGISTRE.agents.values():
        donnees.append({"id": a["modele"], "object": "model", "created": cree, "owned_by": "pixelkreol",
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
    corps = await request.json()
    messages = corps.get("messages") or [{"role": "user", "content": corps.get("texte", "")}]
    agent, methode = await choisir_agent(request.app.state.client, messages)
    return {"agent": agent["id"], "modele": agent["modele"], "nom": agent["nom"], "methode": methode}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    if not autorise(request, API_KEY):
        return erreur(401, "Clé API invalide", "auth")
    try:
        corps = await request.json()
    except json.JSONDecodeError:
        return erreur(400, "Corps JSON invalide", "requete")
    if not isinstance(corps, dict) or not isinstance(corps.get("messages"), list) or not corps["messages"]:
        return erreur(400, "Le champ « messages » (liste non vide) est obligatoire", "requete")
    client: httpx.AsyncClient = request.app.state.client
    modele = str(corps.get("model", "pk-auto"))
    flux = bool(corps.get("stream"))
    REGISTRE.recharger()

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
            return erreur(e.statut if e.statut >= 400 else 502, f"Aucun cerveau disponible pour {agent['nom']} : {e.detail}",
                          "cerveaux_indisponibles")
        reponse["model"] = agent["modele"]
        if annonce and reponse.get("choices"):
            msg = reponse["choices"][0].setdefault("message", {})
            msg["content"] = annonce + (msg.get("content") or "")
        await JOURNAL.ecrire(agent["id"], mode, cerveau, debut, True)
        return JSONResponse(reponse, headers={"X-PK-Agent": agent["id"], "X-PK-Cerveau": cerveau})

    try:
        amont, cerveau = await ouvrir_flux(client, corps_final, chaine)
    except EchecCerveaux as e:
        await JOURNAL.ecrire(agent["id"], mode, "-", debut, False, e.detail)
        return erreur(e.statut if e.statut >= 400 else 502, f"Aucun cerveau disponible pour {agent['nom']} : {e.detail}",
                      "cerveaux_indisponibles")

    async def relais() -> AsyncIterator[bytes]:
        ok = True
        try:
            if annonce:
                yield morceau_sse(agent["modele"], annonce)
            async for bloc in amont.aiter_raw():
                yield bloc
        except httpx.HTTPError as e:
            ok = False
            LOG.warning("flux interrompu : %s", e)
        finally:
            await amont.aclose()
            await JOURNAL.ecrire(agent["id"], mode, cerveau, debut, ok)

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


async def executer_agent(client: httpx.AsyncClient, agent: dict, messages: list[dict]) -> tuple[str, bool]:
    corps, chaine = preparer_corps({"messages": messages, "max_tokens": 1200}, agent)
    debut = time.monotonic()
    try:
        rep, cerveau = await appeler(client, corps, chaine)
        await JOURNAL.ecrire(agent["id"], "equipe", cerveau, debut, True)
        return rep["choices"][0]["message"].get("content") or "", True
    except EchecCerveaux as e:
        await JOURNAL.ecrire(agent["id"], "equipe", "-", debut, False, e.detail)
        return f"(indisponible : {e.detail[:120]})", False


async def mode_equipe(client: httpx.AsyncClient, corps: dict, flux: bool):
    messages = [m for m in corps["messages"] if isinstance(m, dict) and m.get("role") != "system"]
    demande = dernier_message_utilisateur(messages)
    directeur = REGISTRE.agents.get("orchestrateur")
    if not directeur:
        return erreur(500, "L'agent « orchestrateur » est requis pour le mode équipe", "config")

    ids = [a for a in REGISTRE.agents if a != "orchestrateur"]
    consigne_plan = (
        "PLANIFICATION_JSON. Découpe la demande en 1 à "
        f"{MAX_EQUIPE} tâches confiées aux agents les plus adaptés. Réponds UNIQUEMENT avec un objet JSON "
        '{"taches":[{"agent":"<id>","consigne":"<ce que cet agent doit produire>"}]}. '
        f"Identifiants autorisés : {', '.join(ids)}.\n\nDemande : {demande}")
    plan_txt, ok = await executer_agent(client, directeur, [{"role": "user", "content": consigne_plan}])
    plan = extraire_json(plan_txt) if ok else None
    taches: list[tuple[dict, str]] = []
    for t in (plan or {}).get("taches", [])[:MAX_EQUIPE]:
        if isinstance(t, dict) and t.get("agent") in REGISTRE.agents and t["agent"] != "orchestrateur":
            taches.append((REGISTRE.agents[t["agent"]], str(t.get("consigne") or demande)))
    if not taches:  # plan illisible : on se rabat sur les meilleurs agents par mots-clés
        notes = [n for n in classement_mots_cles(demande) if n[1]["id"] != "orchestrateur"]
        taches = [(a, demande) for s, a in notes[:2] if s > 0] or [(REGISTRE.agents[ids[0]], demande)]

    historique = messages[:-1][-6:]
    resultats = await asyncio.gather(*[
        executer_agent(client, a, historique + [{"role": "user", "content": f"{c}\n\n(Demande d'origine : {demande})"}])
        for a, c in taches])

    contributions = "\n\n".join(f"### {a['emoji']} {a['nom']}\n{txt}" for (a, _), (txt, _) in zip(taches, resultats))
    synthese_txt, ok_s = await executer_agent(client, directeur, [{
        "role": "user",
        "content": f"Demande de Will : {demande}\n\nVoici les contributions de l'équipe :\n\n{contributions}\n\n"
                   "Fais la synthèse finale : réponse directe et exploitable, puis les actions et qui s'en charge."}])
    entete = "*👥 Équipe mobilisée : " + ", ".join(f"{a['emoji']} {a['nom']}" for a, _ in taches) + "*\n\n"
    final = entete + (synthese_txt if ok_s else "La synthèse n'a pas pu être faite. Contributions brutes :\n\n" + contributions)
    if not ok_s and not any(ok for _, ok in resultats):
        return erreur(502, "Aucun cerveau disponible pour le mode équipe", "cerveaux_indisponibles")
    final += "\n\n<details><summary>Contributions détaillées</summary>\n\n" + contributions + "\n\n</details>"
    if flux:
        return StreamingResponse(flux_texte("pk-equipe", final), media_type="text/event-stream",
                                 headers={"X-PK-Agent": "equipe"})
    return JSONResponse(completion_simple("pk-equipe", final), headers={"X-PK-Agent": "equipe"})


# =====================================================================
#  Administration : ajouter / retirer un agent personnalisé à chaud
# =====================================================================
def ecrire_perso(agents: list[dict]) -> None:
    chemin = AGENTS_DIR / "perso.yaml"
    contenu = ("# Agents personnalisés (créés via /admin/agents ou la Fabrique d'agents).\n"
               "# Même format que registre.yaml. Rechargé à chaud.\n"
               + yaml.safe_dump({"agents": agents}, sort_keys=False, allow_unicode=True))
    fd, tmp = tempfile.mkstemp(dir=str(AGENTS_DIR), prefix=".perso-", suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(contenu)
    os.replace(tmp, chemin)


def lire_perso() -> list[dict]:
    chemin = AGENTS_DIR / "perso.yaml"
    if not chemin.exists():
        return []
    return (yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}).get("agents") or []


@app.post("/admin/agents")
async def ajouter_agent(request: Request):
    if not autorise(request, ADMIN_KEY):
        return erreur(401, "Clé admin invalide", "auth")
    brut = (await request.body()).decode("utf-8")
    try:
        data = json.loads(brut)
    except json.JSONDecodeError:
        try:
            data = yaml.safe_load(re.sub(r"```(?:yaml)?", "", brut))
        except yaml.YAMLError as e:
            return erreur(400, f"Ni JSON ni YAML valide : {e}", "requete")
    if isinstance(data, list):
        data = data[0] if data else {}
    if isinstance(data, dict) and "agents" in data:
        data = (data["agents"] or [{}])[0]
    if not isinstance(data, dict):
        return erreur(400, "Un agent (objet) est attendu", "requete")
    REGISTRE.recharger()
    if not data.get("num"):  # numérotation automatique à partir de 101 pour les agents perso
        data["num"] = max([a["num"] for a in REGISTRE.agents.values()] + [100]) + 1
    pb = Registre.valider(data, set(REGISTRE.agents), {a["num"] for a in REGISTRE.agents.values()})
    if pb:
        return erreur(422, f"Agent refusé : {pb}", "validation")
    propre = {k: v for k, v in data.items() if not str(k).startswith("_") and k not in ("source", "modele")}
    ecrire_perso(lire_perso() + [propre])
    REGISTRE.recharger(force=True)
    a = REGISTRE.agents.get(propre["id"])
    if not a:
        return erreur(500, "Agent écrit mais non rechargé : " + "; ".join(REGISTRE.avertissements), "config")
    return JSONResponse(status_code=201, content={"ok": True, "modele": a["modele"], "agent": propre["id"]})


@app.delete("/admin/agents/{agent_id}")
async def retirer_agent(agent_id: str, request: Request):
    if not autorise(request, ADMIN_KEY):
        return erreur(401, "Clé admin invalide", "auth")
    perso = lire_perso()
    restants = [a for a in perso if a.get("id") != agent_id]
    if len(restants) == len(perso):
        return erreur(404, "Seuls les agents de perso.yaml peuvent être retirés par l'API", "introuvable")
    ecrire_perso(restants)
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
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bureau IA PixelKréol</title>
<style>:root{{--l:#0B5D6B;--v:#17B6C4;--c:#E8542F;--s:#FFF6E8;--n:#1D2A2E}}
body{{margin:0;font-family:system-ui,sans-serif;background:var(--s);color:var(--n)}}
header{{background:var(--l);color:#fff;padding:28px 16px}}h1{{margin:0 0 6px}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px;padding:16px;max-width:1100px;margin:auto}}
.c{{background:#fff;border-radius:12px;padding:14px;border-top:4px solid var(--v)}}.e{{font-size:28px}}
small{{display:block;color:var(--c);text-transform:uppercase;font-size:11px;letter-spacing:.06em;margin:2px 0 6px}}
p{{margin:0;font-size:14px;line-height:1.4}}</style></head><body>
<header><h1>Bureau IA PixelKréol</h1>{len(REGISTRE.agents)} agents · LLM gratuits · La Réunion</header>
<main>{cartes}</main></body></html>"""
