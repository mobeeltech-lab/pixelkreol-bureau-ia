"""F. Compatibilité OpenAI : /v1/models, /v1/chat/completions (avec et sans flux), erreurs, usage."""
from __future__ import annotations

import json

import httpx
import pytest

from conftest import NB_AGENTS, FauxLiteLLM

OK = FauxLiteLLM.ok


def msg(texte="salut"):
    return [{"role": "user", "content": texte}]


def test_liste_des_modeles(client, h_api):
    r = client.get("/v1/models", headers=h_api)
    j = r.json()
    assert j["object"] == "list" and len(j["data"]) == NB_AGENTS + 2
    ids = [m["id"] for m in j["data"]]
    assert ids[:2] == ["pk-auto", "pk-equipe"] and len(set(ids)) == NB_AGENTS + 2
    assert "pk-02-commercial" in ids and "pk-12-secretariat" in ids
    for m in j["data"]:
        assert m["object"] == "model" and isinstance(m["created"], int) and m["owned_by"]


@pytest.mark.parametrize("modele", ["pk-02-commercial", "commercial", "pk-commercial", " pk-02-commercial "])
def test_les_noms_acceptes_par_n8n_et_open_webui(client, h_api, modele):
    r = client.post("/v1/chat/completions", headers=h_api, json={"model": modele, "messages": msg()})
    assert r.status_code == 200 and r.headers["x-pk-agent"] == "commercial"
    assert r.json()["model"] == "pk-02-commercial"


def test_sans_flux_format_et_usage(client, h_api, faux):
    faux.par_defaut = OK("Bonjour", usage={"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14})
    j = client.post("/v1/chat/completions", headers=h_api, json={"model": "pk-12-secretariat", "messages": msg()}).json()
    assert j["object"] == "chat.completion" and j["choices"][0]["message"] == {"role": "assistant", "content": "Bonjour"}
    assert j["choices"][0]["finish_reason"] == "stop"
    assert j["usage"] == {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}


def test_contenu_en_liste_de_parties_est_accepte(client, h_api):
    r = client.post("/v1/chat/completions", headers=h_api, json={"model": "pk-auto", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Fais un devis"}]}]})
    assert r.status_code == 200


def test_avec_flux_format_sse(client, h_api, faux):
    faux.par_defaut = FauxLiteLLM.flux([FauxLiteLLM.sse("a"), FauxLiteLLM.sse("b"), b"data: [DONE]\n\n"])
    r = client.post("/v1/chat/completions", headers=h_api, json={"model": "pk-12-secretariat", "stream": True, "messages": msg()})
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text.count("data: [DONE]") == 1 and r.text.rstrip().endswith("data: [DONE]")
    assert r.headers["x-pk-agent"] == "secretariat"


def test_annonce_de_l_agent_en_flux_avec_pk_auto(monkeypatch, agents_dir, faux, h_api):
    from fastapi.testclient import TestClient
    from conftest import charger_app
    app = charger_app(monkeypatch, agents_dir, PK_ANNONCE_AGENT="true")
    faux.par_defaut = lambda c: (FauxLiteLLM.flux([FauxLiteLLM.sse("corps"), b"data: [DONE]\n\n"])
                                 if c.get("stream") else OK("devis"))
    with TestClient(app.app) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        r = tc.post("/v1/chat/completions", headers=h_api, json={
            "model": "pk-auto", "stream": True, "messages": msg("Fais un devis pour un site")})
    morceaux = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: {")]
    assert "Devis" in morceaux[0]["choices"][0]["delta"]["content"]


# ---------------------------------------------------------------- erreurs au format OpenAI
def verifie_erreur(r, statut):
    assert r.status_code == statut, r.text
    e = r.json()["error"]
    assert isinstance(e["message"], str) and e["message"] and e["type"] and e["code"] == statut


def test_modele_inconnu(client, h_api):
    verifie_erreur(client.post("/v1/chat/completions", headers=h_api, json={"model": "gpt-4", "messages": msg()}), 404)


@pytest.mark.parametrize("corps", [
    {"model": "pk-auto"}, {"model": "pk-auto", "messages": []}, {"model": "pk-auto", "messages": "salut"},
    {"model": "pk-auto", "messages": ["salut"]}, {"model": "pk-auto", "messages": [1, 2]},
    {"model": "pk-auto", "messages": [None]}, [1, 2, 3],
])
def test_requetes_invalides_donnent_400(client, h_api, corps):
    verifie_erreur(client.post("/v1/chat/completions", headers=h_api, json=corps), 400)


@pytest.mark.parametrize("brut", [b"pas du json", b"", b"\xff\xfe\x81", b"{"])
def test_corps_illisible_donne_400(client, h_api, brut):
    r = client.post("/v1/chat/completions", headers={**h_api, "content-type": "application/json"}, content=brut)
    verifie_erreur(r, 400)


def test_route_corps_illisible_donne_400(client, h_api):
    r = client.post("/route", headers={**h_api, "content-type": "application/json"}, content=b"pas du json")
    verifie_erreur(r, 400)


def test_erreur_interne_inattendue_est_au_format_openai_et_journalisee(client, h_api, monkeypatch, caplog):
    def boum(*a, **k):
        raise RuntimeError("bug interne")
    monkeypatch.setattr(client.appli, "preparer_corps", boum)
    with caplog.at_level("ERROR"):
        r = client.post("/v1/chat/completions", headers=h_api, json={"model": "pk-12-secretariat", "messages": msg()})
    verifie_erreur(r, 500)
    assert "bug interne" not in r.text                                   # pas de détail technique côté client
    assert any("bug interne" in rec.getMessage() or (rec.exc_info and "bug interne" in str(rec.exc_info[1])) for rec in caplog.records)


def test_pk_auto_est_le_modele_par_defaut(client, h_api):
    r = client.post("/v1/chat/completions", headers=h_api, json={"messages": msg("Fais un devis")})
    assert r.status_code == 200


def test_sante_et_vitrine_sans_cle(client):
    assert client.get("/health").json()["agents"] == NB_AGENTS
    assert f"{NB_AGENTS} agents" in client.get("/").text


def test_sante_trace_litellm_injoignable(client, caplog):
    def boum(req):
        raise httpx.ConnectError("litellm éteint")
    client.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(boum))
    with caplog.at_level("WARNING", logger="pk"):
        h = client.get("/health").json()
    assert h["litellm"] is False
    assert any("litellm" in r.getMessage().lower() for r in caplog.records)


def test_date_du_prompt_en_francais(client, h_api, faux):
    client.post("/v1/chat/completions", headers=h_api, json={"model": "pk-12-secretariat", "messages": msg()})
    systeme = faux.appels[0]["messages"][0]["content"]
    jours = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
    ligne = [l for l in systeme.splitlines() if l.startswith("Date et heure")][0]
    assert any(j in ligne for j in jours), ligne


def test_journal_postgres_en_panne_est_trace_sans_bloquer_la_reponse(client, h_api, caplog):
    class PoolCasse:
        def acquire(self):
            raise ConnectionError("base éteinte, mot de passe=cle-litellm-de-test")

        async def close(self):
            pass
    client.appli.JOURNAL.pool = PoolCasse()
    with caplog.at_level("WARNING", logger="pk"):
        for _ in range(3):
            r = client.post("/v1/chat/completions", headers=h_api, json={"model": "pk-12-secretariat", "messages": msg()})
            assert r.status_code == 200
    lignes = [rec.getMessage() for rec in caplog.records if "journal" in rec.getMessage().lower()]
    assert len(lignes) == 1                                # une alerte, pas une par requête
    assert "cle-litellm-de-test" not in lignes[0]
