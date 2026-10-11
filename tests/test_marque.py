"""Éléments propres à PixelKréol rendus configurables (pour l'instance de test ABI Holding).

Règle : sans variable d'environnement, le comportement est EXACTEMENT celui d'avant.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import NB_AGENTS, FauxLiteLLM, charger_app

OK = FauxLiteLLM.ok
VARIABLES = ("PK_MARQUE", "PK_LIEU", "PK_FUSEAU", "PK_PATRON", "PK_VITRINE_PIED", "PK_PROPRIETAIRE")
ABI = {"PK_MARQUE": "ABI Holding", "PK_LIEU": "à Ajaccio", "PK_FUSEAU": "Europe/Paris",
       "PK_PATRON": "la direction d'ABI Holding", "PK_VITRINE_PIED": "environnement de test",
       "PK_PROPRIETAIRE": "abi"}


def pose_equipe(tc, h_api, faux):
    def f(corps):
        user = corps["messages"][-1]["content"]
        if "PLANIFICATION_JSON" in user:
            return OK(json.dumps({"taches": [{"agent": "devis", "consigne": "x"}]}))
        return OK("réponse")
    faux.par_defaut = f
    tc.post("/v1/chat/completions", headers=h_api, json={
        "model": "pk-equipe", "messages": [{"role": "user", "content": "Prépare le lancement"}]})
    return [a for a in faux.appels if "contributions de l'équipe" in a["messages"][-1]["content"]][0]


# ---------------------------------------------------------------- sans variable : rien ne change
def test_valeurs_par_defaut_identiques_a_avant(appli):
    assert appli.MARQUE == "PixelKréol"
    assert appli.LIEU == "à Ajaccio"
    assert appli.FUSEAU == "Europe/Paris" and str(appli.TZ) == "Europe/Paris"
    assert appli.PATRON == "Will"
    assert appli.VITRINE_PIED == "LLM gratuits · La Réunion"
    assert appli.PROPRIETAIRE == "pixelkreol"


def test_pixelkreol_pages_et_textes_inchanges(client, h_api, faux):
    page = client.get("/").text
    assert "<title>Bureau IA PixelKréol</title>" in page
    assert f"<header><h1>Bureau IA PixelKréol</h1>{NB_AGENTS} agents · LLM gratuits · La Réunion</header>" in page
    assert client.app.title == "Bureau IA PixelKréol — Orchestrateur"
    modeles = client.get("/v1/models", headers=h_api).json()["data"]
    assert {m["owned_by"] for m in modeles} == {"pixelkreol"}
    client.post("/v1/chat/completions", headers=h_api, json={"model": "pk-12-secretariat", "messages": [
        {"role": "user", "content": "salut"}]})
    ligne = [l for l in faux.appels[0]["messages"][0]["content"].splitlines() if l.startswith("Date et heure")][0]
    assert ligne.startswith("Date et heure à Ajaccio : ")
    synth = pose_equipe(client, h_api, faux)
    assert synth["messages"][-1]["content"].startswith("Demande de Will : ")


# ---------------------------------------------------------------- avec variables : instance ABI
@pytest.fixture
def client_abi(monkeypatch, agents_dir, faux):
    app = charger_app(monkeypatch, agents_dir, **ABI)
    with TestClient(app.app, raise_server_exceptions=False) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        tc.appli = app
        yield tc


def test_abi_vitrine_sans_trace_de_pixelkreol(client_abi):
    page = client_abi.get("/").text
    assert "<title>Bureau IA ABI Holding</title>" in page
    assert "environnement de test" in page
    assert "PixelKréol" not in page.split("<main>")[0] and "La Réunion" not in page.split("<main>")[0]
    assert client_abi.app.title == "Bureau IA ABI Holding — Orchestrateur"


def test_abi_catalogue_proprietaire(client_abi, h_api):
    assert {m["owned_by"] for m in client_abi.get("/v1/models", headers=h_api).json()["data"]} == {"abi"}


def test_abi_date_fuseau_et_lieu(client_abi, h_api, faux):
    client_abi.post("/v1/chat/completions", headers=h_api, json={"model": "pk-12-secretariat", "messages": [
        {"role": "user", "content": "salut"}]})
    ligne = [l for l in faux.appels[0]["messages"][0]["content"].splitlines() if l.startswith("Date et heure")][0]
    assert ligne.startswith("Date et heure à Ajaccio : ")
    assert str(client_abi.appli.TZ) == "Europe/Paris"


def test_abi_synthese_equipe_ne_parle_pas_de_will(client_abi, h_api, faux):
    synth = pose_equipe(client_abi, h_api, faux)
    assert synth["messages"][-1]["content"].startswith("Demande de la direction d'ABI Holding : ")
    assert "Will" not in synth["messages"][-1]["content"]


def test_fuseau_inconnu_ne_fait_pas_tomber_le_demarrage(monkeypatch, agents_dir):
    app = charger_app(monkeypatch, agents_dir, PK_FUSEAU="Mars/Olympus")
    assert str(app.TZ) == "Europe/Paris"             # retour à la valeur par défaut, avec un avertissement


def test_marque_html_est_echappee(monkeypatch, agents_dir):
    app = charger_app(monkeypatch, agents_dir, PK_MARQUE="<script>alert(1)</script>")
    with TestClient(app.app) as tc:
        assert "<script>alert(1)</script>" not in tc.get("/").text


# ---------------------------------------------------------------- lieu et fuseau dans les fichiers compose
def _env_orchestrateur(chemin, service):
    import re
    import pathlib
    txt = pathlib.Path(chemin).read_text(encoding="utf-8")
    bloc = re.search(r"^  %s:\n(.*?)(?=^  \S|\Z)" % service, txt, re.S | re.M).group(1)
    return dict(re.findall(r"^\s+(PK_LIEU|PK_FUSEAU):\s*(.+?)\s*$", bloc, re.M))


def test_compose_pixelkreol_ajaccio_reglable_par_env():
    env = _env_orchestrateur("docker-compose.yml", "pk_orchestrateur")
    assert env["PK_LIEU"] == "${PK_LIEU:-à Ajaccio}"
    assert env["PK_FUSEAU"] == "${PK_FUSEAU:-Europe/Paris}"


def test_compose_abi_ajaccio_fixe():
    env = _env_orchestrateur("instances/abi/docker-compose.yml", "abi_orchestrateur")
    assert env == {"PK_LIEU": "à Ajaccio", "PK_FUSEAU": "Europe/Paris"}


def test_valeur_par_defaut_du_compose_donne_le_meme_prompt_que_le_code(monkeypatch, agents_dir):
    app = charger_app(monkeypatch, agents_dir, PK_LIEU="à Ajaccio", PK_FUSEAU="Europe/Paris")
    assert (app.LIEU, str(app.TZ)) == ("à Ajaccio", "Europe/Paris")
