"""En-têtes confirmés de LiteLLM : x-litellm-model-name (modèle réel dès la 1re demande), x-litellm-attempted-fallbacks (secours sautés),
ordre des secours relu en arrière-plan, santé « orange » des maillons toujours sautés. Aucune attente ajoutée, aucun coût affiché."""
from __future__ import annotations

import asyncio
import datetime as dt
import time

import httpx
import pytest

from conftest import FauxLiteLLM
from test_fournisseur import INFO, PoolEnregistreur, attendre, brancher, demander, reponse

CHAINE = [{"pk-code": ["pk-code--2", "pk-code--4", "pk-code--5"]}, {"pk-rapide": ["pk-rapide--2"]}]
ENTETES = {"x-litellm-model-id": "hash-1", "x-litellm-model-name": "mistral/codestral-latest",
           "x-litellm-model-api-base": "https://api.mistral.ai/v1", "x-litellm-model-group": "pk-code",
           "x-litellm-attempted-fallbacks": "0", "x-litellm-attempted-retries": "0",
           "x-litellm-response-duration-ms": "263.78", "x-litellm-response-cost": "6.6e-06"}


def maillons(pool):
    return [a for s, a in pool.ecritures if "INTO pk_maillons" in s]


# --- 1. x-litellm-model-name en premier -------------------------------------------------------
def test_model_name_est_lu_en_premier_et_sans_cache(appli):
    assert appli.fournisseur_reel("pk-code", ENTETES) == "mistral/codestral-latest"
    assert appli.fournisseur_reel("pk-code", httpx.Headers({"X-LiteLLM-Model-Name": "groq/qwen/qwen3.8-27b"})) == "groq/qwen/qwen3.8-27b"


def test_sans_model_name_le_repli_est_inchange(appli):
    appli.MODELES_LITELLM["par_id"] = {"hash-1": {"maillon": "pk-code", "modele": "mistral/codestral-latest"}}
    e = {k: v for k, v in ENTETES.items() if k != "x-litellm-model-name"}
    assert appli.fournisseur_reel("pk-code", e) == "mistral/codestral-latest"
    assert appli.fournisseur_reel("pk-code", {"x-litellm-model-api-base": "https://api.mistral.ai/v1"}) == "api.mistral.ai"
    assert appli.fournisseur_reel("pk-code", {"x-litellm-model-name": "  "}) == "pk-code"


def test_un_vrai_modele_dans_la_reponse_n_est_pas_remplace(appli):
    assert appli.fournisseur_reel("gemini-flash-latest", ENTETES) == "gemini-flash-latest"


def test_des_la_premiere_demande_le_journal_montre_le_vrai_modele_sans_lire_model_info(client, h_api, faux):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    compte = brancher(client, faux)
    faux.par_defaut = lambda c: reponse("pk-code", ENTETES)
    demander(client, h_api)
    assert attendre(lambda: pool.fournisseurs() == ["mistral/codestral-latest"])
    assert compte["info"] == 0 and maillons(pool)[0][1] == "mistral/codestral-latest"


def test_flux_et_tester_maintenant_lisent_aussi_model_name(client, h_api, h_admin, faux):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    brancher(client, faux)
    bloc = b'data: {"id":"c","object":"chat.completion.chunk","model":"pk-code","choices":[{"index":0,"delta":{"content":"a"}}]}\n\n'

    def flux(c):
        r = FauxLiteLLM.flux([bloc, b"data: [DONE]\n\n"])
        r.headers["x-litellm-model-name"] = "mistral/codestral-latest"
        return r
    faux.par_defaut = flux
    assert demander(client, h_api, flux=True).status_code == 200
    assert attendre(lambda: pool.fournisseurs() == ["mistral/codestral-latest"])


# --- 2. secours sautés ------------------------------------------------------------------------
@pytest.mark.parametrize("valeur,attendu", [(None, 0), ("", 0), ("abc", 0), ("-3", 0), ("1.5", 0), ("0", 0), ("2", 2), (" 3 ", 3), ("9999", 50)])
def test_attempted_fallbacks_illisible_vaut_zero(appli, valeur, attendu):
    e = {} if valeur is None else {"x-litellm-attempted-fallbacks": valeur}
    assert appli.secours_de(e) == attendu
    assert appli.secours_de(None) == 0
    assert appli.secours_de(httpx.Headers({"X-LiteLLM-Attempted-Fallbacks": "2"})) == 2


def test_ordre_des_secours_trouve_quelle_que_soit_la_forme(appli):
    attendu = {"pk-code": ["pk-code--2", "pk-code--4", "pk-code--5"], "pk-rapide": ["pk-rapide--2"]}
    assert appli._trouver_secours({"fallbacks": CHAINE}) == attendu
    assert appli._trouver_secours({"router_settings": {"fallbacks": CHAINE}, "autre": 1}) == attendu
    assert appli._trouver_secours({"current_values": {"fallbacks": CHAINE}}) == attendu
    assert appli._trouver_secours("router_settings:\n  fallbacks:\n  - pk-code: [pk-code--2]\n") == {"pk-code": ["pk-code--2"]}
    assert appli._trouver_secours({"rien": 1}) == {} and appli._trouver_secours("pas du yaml: [") == {}


def test_maillons_sautes_deduits_de_l_ordre_et_du_groupe_qui_a_repondu(appli):
    appli.CHAINES_LITELLM["par_cerveau"] = {"pk-code": ["pk-code--2", "pk-code--4", "pk-code--5"]}
    e = {"x-litellm-attempted-fallbacks": "2", "x-litellm-model-group": "pk-code--4"}
    assert appli.maillons_sautes("pk-code", e) == (2, ["pk-code", "pk-code--2"], "pk-code--4")
    assert appli.maillons_sautes("pk-code", {"x-litellm-attempted-fallbacks": "0"}) == (0, [], "pk-code")
    assert appli.maillons_sautes("pk-code", {"x-litellm-attempted-fallbacks": "1", "x-litellm-model-group": "inconnu"}) == (1, [], "pk-code")


def test_le_nombre_de_secours_et_les_maillons_sautes_sont_journalises(client, h_api, faux):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    brancher(client, faux)
    client.appli.CHAINES_LITELLM.update(par_cerveau={"pk-code": ["pk-code--2", "pk-code--4"]}, t=time.monotonic(), essai=time.monotonic())
    faux.par_defaut = lambda c: reponse("pk-code", {**ENTETES, "x-litellm-attempted-fallbacks": "2", "x-litellm-model-group": "pk-code--4"})
    demander(client, h_api)
    assert attendre(lambda: len(maillons(pool)) == 3)
    ligne = [a for a in maillons(pool) if len(a) == 7][0]          # l'appel réel : 7 valeurs, dont secours_sautes
    assert ligne[0] == "pk-code--4" and ligne[6] == 2          # le maillon qui a répondu est crédité, pas le cerveau demandé
    assert sorted(a[0] for a in maillons(pool) if len(a) == 2) == ["pk-code", "pk-code--2"]   # les maillons passés par LiteLLM
    journal = [a for s, a in pool.ecritures if "INTO pk_journal" in s][0]
    assert journal[-1] == 2


def test_ordre_illisible_on_garde_le_nombre_sans_nommer_et_sans_erreur(client, h_api, faux):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool

    async def servir(requete):
        if requete.url.path in ("/router/settings", "/config/yaml"):
            return httpx.Response(404, text="non")
        return faux(requete)
    client.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(servir))
    faux.par_defaut = lambda c: reponse("pk-code", {**ENTETES, "x-litellm-attempted-fallbacks": "1", "x-litellm-model-group": "pk-code--2"})
    assert demander(client, h_api).status_code == 200
    assert attendre(lambda: len(maillons(pool)) == 1)
    time.sleep(0.2)
    assert len(maillons(pool)) == 1 and maillons(pool)[0][6] == 1


def test_l_ordre_est_relu_en_arriere_plan_une_fois_par_fenetre(client, h_api, faux):
    appels = []

    async def servir(requete):
        if requete.url.path == "/router/settings":
            appels.append(1)
            return httpx.Response(200, json={"fallbacks": CHAINE})
        return faux(requete)
    client.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(servir))
    client.appli.JOURNAL.pool = pool = PoolEnregistreur()
    faux.par_defaut = lambda c: reponse("pk-code", {**ENTETES, "x-litellm-attempted-fallbacks": "1", "x-litellm-model-group": "pk-code--2"})
    for _ in range(3):
        demander(client, h_api)
        time.sleep(0.15)
    assert attendre(lambda: client.appli.CHAINES_LITELLM["par_cerveau"].get("pk-code"))
    assert len(appels) == 1
    demander(client, h_api)
    assert attendre(lambda: any("'saute'" in s for s, _ in pool.ecritures))


def test_router_settings_lent_ne_retarde_pas_la_reponse(client, h_api, faux):
    async def servir(requete):
        if requete.url.path == "/router/settings":
            await asyncio.sleep(30)
        return faux(requete)
    client.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(servir))
    faux.par_defaut = lambda c: reponse("pk-code", {**ENTETES, "x-litellm-attempted-fallbacks": "1", "x-litellm-model-group": "pk-code--2"})
    debut = time.monotonic()
    assert demander(client, h_api).status_code == 200
    assert time.monotonic() - debut < 2.0


def test_aucun_cout_ni_cle_dans_le_journal(client, h_api, faux):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    brancher(client, faux)
    faux.par_defaut = lambda c: reponse("pk-code", ENTETES)
    demander(client, h_api)
    assert attendre(lambda: pool.fournisseurs())
    assert "6.6e-06" not in repr(pool.ecritures) and "response-cost" not in repr(pool.ecritures)


# --- mise à niveau des tables existantes ------------------------------------------------------
def test_les_colonnes_sont_ajoutees_sans_toucher_aux_donnees(appli):
    class Pool(PoolEnregistreur):
        pass
    pool = Pool()

    async def go():
        import asyncpg
        async def creer(*a, **k):
            return pool
        asyncpg.create_pool = creer
        appli.DATABASE_URL = "postgresql://x"
        appli.JOURNAL.pool = None
        await appli.JOURNAL.demarrer()
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        pytest.skip("asyncpg absent")
    avant = None
    import asyncpg as ap
    avant = ap.create_pool
    try:
        asyncio.run(go())
    finally:
        ap.create_pool = avant
    sql = " ".join(s for s, _ in pool.ecritures)
    assert "ALTER TABLE pk_journal ADD COLUMN IF NOT EXISTS secours INTEGER" in sql
    assert "ALTER TABLE pk_maillons ADD COLUMN IF NOT EXISTS secours_sautes INTEGER" in sql
    assert "DROP" not in sql.upper() and "DELETE" not in sql.upper().split("CREATE")[0]


# --- santé : orange pour un maillon toujours sauté, rouge réservé aux 402/404 ---------------------
class FausseBase:
    def __init__(self, dernier=(), ok=(), j24=(), depuis=(), sautes=()):
        self.jeux = [list(dernier), list(ok), list(j24), list(depuis), list(sautes)]

    async def fetch(self, sql, *a):
        return self.jeux.pop(0)


def il_y_a(h):
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=h)


def sante(appli, **jeux):
    return {x["maillon"]: x for x in asyncio.run(appli._sante_maillons(FausseBase(**jeux)))}


def test_maillon_saute_depuis_plus_de_24h_et_jamais_ok_passe_en_orange(appli):
    s = sante(appli,
              dernier=[{"maillon": "pk-code", "ts": il_y_a(1), "statut": 200, "resultat": "ok", "source": "trafic", "fournisseur": "m"}],
              sautes=[{"maillon": "pk-code--2", "sautes_24h": 7, "depuis": il_y_a(30), "dernier": il_y_a(1)}])
    assert s["pk-code--2"]["suspect"] is True and s["pk-code--2"]["mort"] is False and s["pk-code--2"]["sautes_24h"] == 7
    assert s["pk-code--2"]["dernier"]["resultat"] == "sauté"
    assert s["pk-code"]["suspect"] is False and s["pk-code"]["sautes_24h"] == 0
    assert list(s)[0] == "pk-code--2"                       # les maillons à surveiller en tête


def test_saute_depuis_moins_de_24h_n_est_pas_orange(appli):
    s = sante(appli, sautes=[{"maillon": "pk-code--2", "sautes_24h": 3, "depuis": il_y_a(5), "dernier": il_y_a(1)}])
    assert s["pk-code--2"]["suspect"] is False and s["pk-code--2"]["sautes_24h"] == 3


def test_le_rouge_reste_reserve_aux_402_404_vus_directement(appli):
    s = sante(appli,
              dernier=[{"maillon": "pk-x", "ts": il_y_a(1), "statut": 404, "resultat": "404", "source": "test", "fournisseur": ""}],
              depuis=[{"maillon": "pk-x", "depuis": il_y_a(40)}],
              sautes=[{"maillon": "pk-x", "sautes_24h": 4, "depuis": il_y_a(40), "dernier": il_y_a(1)}])
    assert s["pk-x"]["mort"] is True and s["pk-x"]["suspect"] is False


def test_dernier_et_dernier_ok_n_ignorent_que_les_lignes_sautees(appli):
    import inspect
    src = inspect.getsource(appli._sante_maillons)
    assert "source <> 'saute'" in src


# --- l'ordre vient d'abord de agents/cerveaux.yaml ------------------------------------------------
def charges(appli, **par_nom):
    """Modèles que LiteLLM a chargés (comme /model/info) : un fournisseur sans clé n'y figure pas."""
    appli.MODELES_LITELLM["par_id"] = {f"id-{n}": {"maillon": n, "modele": m} for n, m in par_nom.items()}
    appli.MODELES_LITELLM["t"] = appli.MODELES_LITELLM["essai"] = time.monotonic()


CHARGES = {"pk-code": "mistral/codestral-latest", "pk-code--2": "groq/qwen/qwen3.8-27b", "pk-code--3": "groq/openai/gpt-oss-120b"}


def test_la_chaine_de_cerveaux_yaml_est_lue_dans_l_ordre(appli):
    assert appli.REGISTRE.modeles_du_cerveau("pk-code")[:4] == [
        "mistral/codestral-latest", "groq/qwen/qwen3.8-27b", "openrouter/qwen/qwen3.8-27b:free", "groq/openai/gpt-oss-120b"]
    assert appli.REGISTRE.modeles_du_cerveau("inconnu") == []


def test_maillons_sautes_par_cerveaux_yaml_en_ignorant_les_fournisseurs_sans_cle(appli):
    charges(appli, **CHARGES)   # openrouter/qwen…:free absent : pas de clé OpenRouter
    e = {"x-litellm-model-name": "groq/openai/gpt-oss-120b", "x-litellm-attempted-fallbacks": "2", "x-litellm-model-group": "pk-code--3"}
    assert appli.maillons_sautes("pk-code", e) == (2, ["pk-code", "pk-code--2"], "pk-code--3")
    e = {"x-litellm-model-name": "groq/qwen/qwen3.8-27b", "x-litellm-attempted-fallbacks": "1"}
    assert appli.maillons_sautes("pk-code", e) == (1, ["pk-code"], "pk-code--2")           # même sans x-litellm-model-group
    e = {"x-litellm-model-name": "mistral/codestral-latest", "x-litellm-attempted-fallbacks": "1"}
    assert appli.maillons_sautes("pk-code", e) == (1, [], "pk-code")


def test_cerveaux_yaml_prime_sur_le_routeur(appli):
    charges(appli, **CHARGES)
    appli.CHAINES_LITELLM["par_cerveau"] = {"pk-code": ["autre-1", "autre-2"]}
    e = {"x-litellm-model-name": "groq/openai/gpt-oss-120b", "x-litellm-attempted-fallbacks": "2", "x-litellm-model-group": "autre-2"}
    assert appli.maillons_sautes("pk-code", e)[1] == ["pk-code", "pk-code--2"]


def test_sans_correspondance_on_se_rabat_sur_le_routeur_puis_sur_le_nombre(appli):
    charges(appli, **CHARGES)
    appli.CHAINES_LITELLM["par_cerveau"] = {"pk-code": ["pk-code--2", "pk-code--3"]}
    e = {"x-litellm-model-name": "modele/inconnu", "x-litellm-attempted-fallbacks": "2", "x-litellm-model-group": "pk-code--3"}
    assert appli.maillons_sautes("pk-code", e) == (2, ["pk-code", "pk-code--2"], "pk-code--3")
    appli.CHAINES_LITELLM["par_cerveau"] = {}
    assert appli.maillons_sautes("pk-code", e) == (2, [], "pk-code--3")


def test_de_bout_en_bout_l_ordre_vient_de_cerveaux_yaml_sans_toucher_au_routeur(client, h_api, faux):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    vus = []
    info = {"data": [{"model_name": n, "litellm_params": {"model": m}, "model_info": {"id": f"id-{n}"}} for n, m in CHARGES.items()]}

    async def servir(requete):
        vus.append(requete.url.path)
        if requete.url.path.endswith("/model/info"):
            return httpx.Response(200, json=info)
        return faux(requete)
    client.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(servir))
    client.appli.MODELES_LITELLM["par_id"] = {f"id-{n}": {"maillon": n, "modele": m} for n, m in CHARGES.items()}
    faux.par_defaut = lambda c: reponse("pk-code", {**ENTETES, "x-litellm-model-name": "groq/openai/gpt-oss-120b",
                                                    "x-litellm-attempted-fallbacks": "2", "x-litellm-model-group": "pk-code--3"})
    demander(client, h_api)
    assert attendre(lambda: len(maillons(pool)) == 3)
    assert sorted(a[0] for a in maillons(pool) if len(a) == 2) == ["pk-code", "pk-code--2"]
    assert not any(p in ("/router/settings", "/config/yaml") for p in vus)


# --- /config/yaml : on n'en garde que router_settings.fallbacks, en mémoire --------------------------------
CLE_FICTIVE = "sk" + "-fictive-" + "A1b2C3d4E5f6"
CLE_FOURNISSEUR = "gsk" + "_fictive_" + "Z9y8X7w6V5"
CONFIG_COMPLETE = {
    "model_list": [{"model_name": "pk-code", "litellm_params": {"model": "mistral/codestral-latest", "api_key": CLE_FOURNISSEUR}}],
    "router_settings": {"fallbacks": CHAINE, "num_retries": 0, "redis_password": CLE_FICTIVE},
    "litellm_settings": {"fallbacks": [{"pk-x": ["pk-y"]}], "master_key": CLE_FICTIVE},
    "general_settings": {"master_key": CLE_FICTIVE, "database_url": "postgresql://u:" + CLE_FICTIVE + "@h/db"},
}


@pytest.mark.parametrize("forme", ["json", "yaml"])
def test_config_yaml_ne_garde_que_router_settings_fallbacks(appli, forme):
    import yaml
    corps = CONFIG_COMPLETE if forme == "json" else yaml.safe_dump(CONFIG_COMPLETE)
    table = appli._trouver_secours(corps, seulement_router=True)
    assert table == {"pk-code": ["pk-code--2", "pk-code--4", "pk-code--5"], "pk-rapide": ["pk-rapide--2"]}
    assert CLE_FICTIVE not in repr(table) and CLE_FOURNISSEUR not in repr(table)


def test_config_yaml_aucune_cle_stockee_journalisee_ni_renvoyee(client, h_api, h_admin, faux, caplog):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    vus = []

    async def servir(requete):
        vus.append(requete.url.path)
        if requete.url.path == "/router/settings":
            return httpx.Response(404, text="non")
        if requete.url.path == "/config/yaml":
            return httpx.Response(200, json=CONFIG_COMPLETE)
        return faux(requete)
    client.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(servir))
    faux.par_defaut = lambda c: reponse("pk-code", {**ENTETES, "x-litellm-model-name": "modele/inconnu", "x-litellm-attempted-fallbacks": "2",
                                                    "x-litellm-model-group": "pk-code--4"})
    with caplog.at_level("DEBUG"):
        demander(client, h_api)
        assert attendre(lambda: client.appli.CHAINES_LITELLM["par_cerveau"].get("pk-code"))
        demander(client, h_api)
        assert attendre(lambda: any(len(a) == 2 for a in maillons(pool)))
    assert "/config/yaml" in vus
    assert client.appli.CHAINES_LITELLM["par_cerveau"]["pk-code"] == ["pk-code--2", "pk-code--4", "pk-code--5"]
    for secret in (CLE_FICTIVE, CLE_FOURNISSEUR):
        assert secret not in repr(client.appli.CHAINES_LITELLM)                       # rien en mémoire hors des noms de maillons
        assert secret not in repr(pool.ecritures)                                     # rien en base
        assert secret not in caplog.text                                              # rien dans les journaux
        assert secret not in client.get("/admin/activite", headers=h_admin).text      # rien renvoyé au dashboard
    assert set(client.appli.CHAINES_LITELLM["par_cerveau"]) == {"pk-code", "pk-rapide"}   # le fallbacks de litellm_settings n'est pas retenu
