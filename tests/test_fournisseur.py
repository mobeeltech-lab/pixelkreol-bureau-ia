"""Fournisseur affiché : quand LiteLLM renvoie le nom du cerveau (« pk-code ») dans le champ « model », on lit ses en-têtes de
réponse (x-litellm-model-id traduit par /model/info mis en cache, sinon x-litellm-model-api-base). Aucune attente ajoutée."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from conftest import FauxLiteLLM

OK = FauxLiteLLM.ok
INFO = {"data": [
    {"model_name": "pk-code", "litellm_params": {"model": "mistral/codestral-latest", "api_key": "os.environ/MISTRAL_API_KEY"}, "model_info": {"id": "id-mistral"}},
    {"model_name": "pk-rapide--2", "litellm_params": {"model": "groq/qwen/qwen3.8-27b"}, "model_info": {"id": "id-groq"}},
    {"model_name": "pk-long", "litellm_params": {"model": "gemini/gemini-flash-latest"}, "model_info": {"id": "id-gemini"}}]}


class PoolEnregistreur:
    def __init__(self):
        self.ecritures = []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, *args):
        self.ecritures.append((sql, args))

    async def close(self):
        pass

    def fournisseurs(self):
        return [a[-1] for s, a in self.ecritures if "INTO pk_journal" in s]


def reponse(modele, entetes=None):
    corps = {**OK().json(), "model": modele}
    return httpx.Response(200, json=corps, headers=entetes or {})


def brancher(client, faux, info=None, lent=0.0):
    """Faux LiteLLM + /model/info (compté). `lent` : délai de /model/info (secondes)."""
    compte = {"info": 0}

    async def servir(requete: httpx.Request):
        if requete.url.path.endswith("/model/info"):
            compte["info"] += 1
            if lent:
                await asyncio.sleep(lent)
            return httpx.Response(200, json=info if info is not None else INFO)
        return faux(requete)
    client.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(servir))
    return compte


def attendre(cond, delai=3.0):
    fin = time.time() + delai
    while time.time() < fin:
        if cond():
            return True
        time.sleep(0.02)
    return False


def demander(client, h_api, modele="pk-06-developpeur", flux=False):
    return client.post("/v1/chat/completions", headers=h_api,
                       json={"model": modele, "stream": flux, "messages": [{"role": "user", "content": "salut"}]})


# --- la fonction seule ------------------------------------------------------------------------
def test_un_vrai_nom_de_modele_est_garde_tel_quel(appli):
    assert appli.fournisseur_reel("gemini-flash-latest", {"x-litellm-model-id": "id-groq"}) == "gemini-flash-latest"


def test_un_nom_de_cerveau_est_traduit_par_l_identifiant_du_deploiement(appli):
    appli.MODELES_LITELLM["par_id"] = {"id-groq": {"maillon": "pk-rapide--2", "modele": "groq/qwen/qwen3.8-27b"}}
    assert appli.fournisseur_reel("pk-code", {"x-litellm-model-id": "id-groq"}) == "groq/qwen/qwen3.8-27b"


def test_sans_cache_on_se_rabat_sur_l_adresse_du_fournisseur(appli):
    e = {"x-litellm-model-id": "inconnu", "x-litellm-model-api-base": "https://api.groq.com/openai/v1"}
    assert appli.fournisseur_reel("pk-code", e) == "api.groq.com"


def test_sans_aucun_en_tete_on_garde_ce_que_la_reponse_disait(appli):
    assert appli.fournisseur_reel("pk-code", {}) == "pk-code"
    assert appli.fournisseur_reel(None, None) == ""
    assert appli.fournisseur_reel("pk-code", httpx.Headers({"X-LiteLLM-Model-Id": "x"})) == "pk-code"   # en-têtes httpx : casse indifférente


# --- de bout en bout --------------------------------------------------------------------------
def test_le_vrai_modele_apparait_dans_le_journal_apres_lecture_de_model_info(client, h_api, faux):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    compte = brancher(client, faux)
    faux.par_defaut = lambda c: reponse("pk-code", {"x-litellm-model-id": "id-mistral", "x-litellm-model-api-base": "https://api.mistral.ai/v1"})
    demander(client, h_api)                                      # 1re demande : cache vide -> adresse du fournisseur, /model/info part en arrière-plan
    assert attendre(lambda: compte["info"] == 1 and client.appli.MODELES_LITELLM["par_id"])
    demander(client, h_api)                                      # 2e demande : le cache répond
    assert attendre(lambda: len(pool.fournisseurs()) == 2)
    assert pool.fournisseurs() == ["api.mistral.ai", "mistral/codestral-latest"]
    assert compte["info"] == 1                                   # pas de relecture à chaque demande


def test_gemini_garde_son_nom_et_ne_declenche_aucune_lecture(client, h_api, faux):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    compte = brancher(client, faux)
    faux.par_defaut = lambda c: reponse("gemini-flash-latest", {"x-litellm-model-id": "id-gemini"})
    demander(client, h_api)
    assert attendre(lambda: pool.fournisseurs() == ["gemini-flash-latest"])
    assert compte["info"] == 0


def test_flux_meme_correction(client, h_api, faux):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    brancher(client, faux)
    client.appli.MODELES_LITELLM["par_id"] = {"id-groq": {"maillon": "pk-rapide--2", "modele": "groq/qwen/qwen3.8-27b"}}
    bloc = b'data: {"id":"c","object":"chat.completion.chunk","model":"pk-code","choices":[{"index":0,"delta":{"content":"a"}}]}\n\n'

    def flux(c):
        r = FauxLiteLLM.flux([bloc, b"data: [DONE]\n\n"])
        r.headers["x-litellm-model-id"] = "id-groq"
        return r
    faux.par_defaut = flux
    assert demander(client, h_api, flux=True).status_code == 200
    assert attendre(lambda: pool.fournisseurs() == ["groq/qwen/qwen3.8-27b"])


def test_model_info_lent_ou_en_panne_ne_retarde_pas_la_reponse(client, h_api, faux):
    brancher(client, faux, lent=30)
    faux.par_defaut = lambda c: reponse("pk-code", {"x-litellm-model-id": "id-mistral"})
    debut = time.monotonic()
    assert demander(client, h_api).status_code == 200
    assert time.monotonic() - debut < 2.0


def test_model_info_relu_au_plus_toutes_les_10_minutes(client, h_api, faux):
    compte = brancher(client, faux, info={"data": []})           # réponse sans table : le cache reste vide
    faux.par_defaut = lambda c: reponse("pk-code", {"x-litellm-model-id": "id-inconnu"})
    for _ in range(4):
        demander(client, h_api)
        time.sleep(0.15)
    assert compte["info"] == 1                                   # une seule lecture malgré 4 demandes sans correspondance
    client.appli.MODELES_LITELLM["essai"] = time.monotonic() - 61
    demander(client, h_api)
    assert attendre(lambda: compte["info"] == 2)                 # cache vide : nouvel essai après 1 min


def test_model_info_en_erreur_ne_casse_rien(client, h_api, faux):
    async def servir(requete):
        if requete.url.path.endswith("/model/info"):
            return httpx.Response(500, text="boum")
        return faux(requete)
    client.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(servir))
    faux.par_defaut = lambda c: reponse("pk-code", {"x-litellm-model-id": "id-x", "x-litellm-model-api-base": "https://api.groq.com/openai/v1"})
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    assert demander(client, h_api).status_code == 200
    assert attendre(lambda: pool.fournisseurs() == ["api.groq.com"])


def test_aucune_cle_de_model_info_n_est_conservee(client, h_api, faux):
    brancher(client, faux)
    faux.par_defaut = lambda c: reponse("pk-code", {"x-litellm-model-id": "id-mistral"})
    demander(client, h_api)
    assert attendre(lambda: client.appli.MODELES_LITELLM["par_id"])
    assert "api_key" not in repr(client.appli.MODELES_LITELLM["par_id"]) and "MISTRAL_API_KEY" not in repr(client.appli.MODELES_LITELLM["par_id"])
