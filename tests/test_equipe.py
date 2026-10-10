"""B. pk-equipe : découpage, parallélisme, limite PK_EQUIPE_MAX, synthèse, pannes."""
from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FauxLiteLLM, charger_app

OK = FauxLiteLLM.ok


def poste(corps):
    """Nom de l'agent qui répond (d'après la ligne « ## Ton poste » de sa fiche)."""
    return re.search(r"## Ton poste : \S+ ([^\n]+)", corps["messages"][0]["content"]).group(1)


def plan(*agents):
    return OK(json.dumps({"taches": [{"agent": a, "consigne": f"fais ta part ({a})"} for a in agents]}))


def pose(client, h_api, texte="Prépare le lancement du site de la boulangerie", stream=False):
    return client.post("/v1/chat/completions", headers=h_api,
                       json={"model": "pk-equipe", "stream": stream, "messages": [{"role": "user", "content": texte}]})


def reponses_par_role(faux, plan_rep, synthese="SYNTHÈSE FINALE"):
    """Répond selon ce que le message demande (plan / contribution d'un agent / synthèse)."""
    def f(corps):
        user = corps["messages"][-1]["content"]
        if "PLANIFICATION_JSON" in user:
            return plan_rep
        if "contributions de l'équipe" in user:
            return OK(synthese)
        return OK("contribution")
    faux.par_defaut = f


def test_equipe_nominal(client, h_api, faux):
    reponses_par_role(faux, plan("devis", "redacteur-seo"))
    r = pose(client, h_api)
    assert r.status_code == 200
    texte = r.json()["choices"][0]["message"]["content"]
    assert "SYNTHÈSE FINALE" in texte and "Devis" in texte
    assert len(faux.appels) == 4  # plan + 2 agents + synthèse


def test_limite_pk_equipe_max(monkeypatch, agents_dir, faux, h_api):
    app = charger_app(monkeypatch, agents_dir, PK_EQUIPE_MAX="2")
    with TestClient(app.app, raise_server_exceptions=False) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        reponses_par_role(faux, plan("devis", "juriste", "comptable", "support", "devops"))
        r = pose(tc, h_api)
    assert r.status_code == 200 and len(faux.appels) == 4  # plan + 2 agents (pas 5) + synthèse


@pytest.mark.parametrize("valeur", ["0", "-3"])
def test_pk_equipe_max_absurde_reste_utilisable(monkeypatch, agents_dir, faux, h_api, valeur):
    app = charger_app(monkeypatch, agents_dir, PK_EQUIPE_MAX=valeur)
    with TestClient(app.app, raise_server_exceptions=False) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        reponses_par_role(faux, plan("devis", "juriste"))
        r = pose(tc, h_api)
    assert r.status_code == 200 and len(faux.appels) >= 3


def test_pk_equipe_max_illisible_ne_fait_pas_tomber_le_demarrage(monkeypatch, agents_dir):
    app = charger_app(monkeypatch, agents_dir, PK_EQUIPE_MAX="beaucoup")
    assert app.MAX_EQUIPE >= 1


def test_agents_en_parallele(monkeypatch, agents_dir, h_api):
    app = charger_app(monkeypatch, agents_dir)
    etat = {"en_cours": 0, "max": 0}

    async def handler(req: httpx.Request):
        corps = json.loads(req.content)
        user = corps["messages"][-1]["content"]
        if "PLANIFICATION_JSON" in user:
            return plan("devis", "juriste", "comptable")
        if "contributions de l'équipe" in user:
            return OK("synthèse")
        etat["en_cours"] += 1
        etat["max"] = max(etat["max"], etat["en_cours"])
        await asyncio.sleep(0.15)
        etat["en_cours"] -= 1
        return OK("contrib")
    with TestClient(app.app) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert pose(tc, h_api).status_code == 200
    assert etat["max"] == 3


def test_agent_en_panne_est_dit_dans_la_reponse_finale(client, h_api, faux):
    def f(corps):
        sys_txt, user = corps["messages"][0]["content"], corps["messages"][-1]["content"]
        if "PLANIFICATION_JSON" in user:
            return plan("devis", "juriste")
        if "contributions de l'équipe" in user:
            return OK("SYNTHÈSE FINALE")
        if poste(corps) == "Juriste":
            return FauxLiteLLM.statut(503, "juriste en panne")
        return OK("contribution du devis")
    faux.par_defaut = f
    texte = pose(client, h_api).json()["choices"][0]["message"]["content"]
    assert "SYNTHÈSE FINALE" in texte
    # le texte visible (avant les « détails ») doit prévenir que le Juriste n'a pas répondu
    visible = texte.split("<details>")[0]
    assert "Juriste" in visible and ("n'a pas répondu" in visible or "sans réponse" in visible)
    # et la synthèse a été prévenue (pour ne pas inventer la partie manquante)
    synth = [a for a in faux.appels if "contributions de l'équipe" in a["messages"][-1]["content"]][0]
    assert "Juriste" in synth["messages"][-1]["content"] and "indisponible" in synth["messages"][-1]["content"].lower()


def test_agent_qui_depasse_le_delai_est_dit(client, h_api, faux):
    def f(corps):
        sys_txt, user = corps["messages"][0]["content"], corps["messages"][-1]["content"]
        if "PLANIFICATION_JSON" in user:
            return plan("devis", "juriste")
        if "contributions de l'équipe" in user:
            return OK("SYNTHÈSE FINALE")
        if poste(corps) == "Juriste":
            return httpx.ReadTimeout("trop long")
        return OK("contribution du devis")
    faux.par_defaut = f
    visible = pose(client, h_api).json()["choices"][0]["message"]["content"].split("<details>")[0]
    assert "Juriste" in visible and ("n'a pas répondu" in visible or "sans réponse" in visible)


def test_tous_les_agents_en_panne_donne_une_erreur_pas_une_fausse_synthese(client, h_api, faux):
    def f(corps):
        user = corps["messages"][-1]["content"]
        if "PLANIFICATION_JSON" in user:
            return plan("devis", "juriste")
        if "contributions de l'équipe" in user:
            return OK("synthèse inventée à partir de rien")
        return FauxLiteLLM.statut(503, "off")
    faux.par_defaut = f
    r = pose(client, h_api)
    assert r.status_code == 502 and "synthèse inventée" not in r.text


def test_reponse_bizarre_d_un_agent_ne_fait_pas_tout_tomber(client, h_api, faux):
    def f(corps):
        sys_txt, user = corps["messages"][0]["content"], corps["messages"][-1]["content"]
        if "PLANIFICATION_JSON" in user:
            return plan("devis", "juriste")
        if "contributions de l'équipe" in user:
            return OK("SYNTHÈSE FINALE")
        if poste(corps) == "Juriste":
            return httpx.Response(200, json={"choices": []})   # 200 mais sans contenu exploitable
        return OK("contribution du devis")
    faux.par_defaut = f
    r = pose(client, h_api)
    assert r.status_code == 200 and "SYNTHÈSE FINALE" in r.text
    assert "Juriste" in r.json()["choices"][0]["message"]["content"].split("<details>")[0]


@pytest.mark.parametrize("plan_rep", [
    OK("pas du json du tout"),
    OK('{"taches": null}'),
    OK('{"taches": "devis"}'),
    OK('{"taches": [1, "x", null, {"agent": ["liste"]}, {"agent": "inexistant"}, {"agent": "orchestrateur"}]}'),
    OK("[1, 2]"),
    FauxLiteLLM.statut(500, "directeur en panne"),
])
def test_plan_illisible_se_rabat_sur_les_mots_cles(client, h_api, faux, plan_rep):
    reponses_par_role(faux, plan_rep)
    r = pose(client, h_api, texte="Rédige un devis puis relis le contrat")
    assert r.status_code == 200, r.text


def test_meme_agent_cite_deux_fois_ne_travaille_qu_une_fois(client, h_api, faux):
    reponses_par_role(faux, plan("devis", "devis", "devis"))
    assert pose(client, h_api).status_code == 200
    assert len(faux.appels) == 3  # plan + 1 agent + synthèse


def test_synthese_en_panne_donne_les_contributions_une_seule_fois(client, h_api, faux):
    def f(corps):
        user = corps["messages"][-1]["content"]
        if "PLANIFICATION_JSON" in user:
            return plan("devis")
        if "contributions de l'équipe" in user:
            return FauxLiteLLM.statut(503, "off")
        return OK("CONTRIBUTION-UNIQUE")
    faux.par_defaut = f
    r = pose(client, h_api)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"].count("CONTRIBUTION-UNIQUE") == 1
    assert "synthèse n'a pas pu être faite" in r.json()["choices"][0]["message"]["content"]


def test_equipe_usage_additionne_les_appels(client, h_api, faux):
    reponses_par_role(faux, plan("devis", "juriste"))
    u = pose(client, h_api).json()["usage"]
    assert u["total_tokens"] == 12 * 4 and u["prompt_tokens"] == 5 * 4


def test_equipe_en_flux(client, h_api, faux):
    reponses_par_role(faux, plan("devis"))
    r = pose(client, h_api, stream=True)
    assert r.status_code == 200 and r.text.rstrip().endswith("data: [DONE]")
    assert "SYNTH" in r.text


def test_delai_par_agent_en_mode_equipe(monkeypatch, agents_dir, h_api):
    """Un agent qui n'a pas fini dans PK_EQUIPE_DELAI secondes est abandonné ET signalé."""
    app = charger_app(monkeypatch, agents_dir, PK_EQUIPE_DELAI="1")

    async def handler(req: httpx.Request):
        corps = json.loads(req.content)
        user = corps["messages"][-1]["content"]
        if "PLANIFICATION_JSON" in user:
            return plan("devis", "juriste")
        if "contributions de l'équipe" in user:
            return OK("SYNTHÈSE FINALE")
        if poste(corps) == "Juriste":
            await asyncio.sleep(5)
        return OK("contribution")
    with TestClient(app.app) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        r = pose(tc, h_api)
    visible = r.json()["choices"][0]["message"]["content"].split("<details>")[0]
    assert r.status_code == 200 and "sans réponse de : Juriste" in visible
