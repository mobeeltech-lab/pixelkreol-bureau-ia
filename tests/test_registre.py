"""D. Registre à chaud : perso.yaml, POST /admin/agents, YAML invalide, écriture atomique."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import charger_app

NOUVEL = {"id": "garagiste", "nom": "Garagiste", "emoji": "🔧", "cerveau": "pk-rapide",
          "description": "Aide un garage automobile.", "consignes": "Tu aides un garage.",
          "mots_cles": ["vidange", "pneus"]}


def nb_agents(client, h_api):
    return len(client.get("/v1/models", headers=h_api).json()["data"]) - 2


def ecrire(agents_dir: Path, nom: str, contenu):
    (agents_dir / nom).write_bytes(contenu if isinstance(contenu, bytes) else contenu.encode())


# ---------------------------------------------------------------- lecture à chaud
def test_25_agents_au_depart(client, h_api):
    assert nb_agents(client, h_api) == 25


@pytest.mark.parametrize("contenu", [
    "agents: [ceci n'est pas : du yaml valide\n  - :",
    "- id: a\n- id: b\n",                          # racine = liste
    "juste du texte",                               # racine = texte
    "agents: {id: x}",                              # agents = dictionnaire
    "agents: 12",
    b"\xff\xfe\x00 pas de l'utf-8 \x81\x82",          # octets illisibles
    "agents:\n  - id: [a, b]\n    num: 130\n    nom: X\n    cerveau: pk-rapide\n    description: d\n    consignes: c\n",
    "agents:\n  - {id: x1, num: 131, nom: X, cerveau: [pk-rapide], description: d, consignes: c}\n",
    "agents:\n  - {id: x2, num: 132, nom: X, cerveau: pk-rapide, famille: [a], description: d, consignes: c}\n",
    "agents:\n  - {id: x3, num: [1], nom: X, cerveau: pk-rapide, description: d, consignes: c}\n",
])
def test_perso_casse_ne_fait_jamais_tomber_les_25_agents(client, h_api, agents_dir, contenu):
    ecrire(agents_dir, "perso.yaml", contenu)
    os.utime(agents_dir / "perso.yaml", None)
    r = client.get("/v1/models", headers=h_api)
    assert r.status_code == 200 and len(r.json()["data"]) - 2 == 25
    assert client.get("/health").json()["agents"] == 25
    assert client.get("/health").json()["avertissements"]      # le problème est signalé
    # le routage et la conversation fonctionnent encore
    assert client.post("/route", headers=h_api, json={"texte": "fais un devis"}).status_code == 200
    assert client.post("/v1/chat/completions", headers=h_api, json={
        "model": "pk-auto", "messages": [{"role": "user", "content": "devis site"}]}).status_code == 200


def test_perso_corrige_apres_erreur_redevient_lisible(client, h_api, agents_dir):
    ecrire(agents_dir, "perso.yaml", "agents: [cassé : :")
    assert nb_agents(client, h_api) == 25
    ecrire(agents_dir, "perso.yaml", yaml.safe_dump({"agents": [dict(NOUVEL, num=130)]}))
    os.utime(agents_dir / "perso.yaml", ns=(1, 2_000_000_000_000_000_000))
    assert nb_agents(client, h_api) == 26


def test_perso_valide_puis_casse_garde_les_agents_clients_deja_charges(client, h_api, agents_dir):
    ecrire(agents_dir, "perso.yaml", yaml.safe_dump({"agents": [dict(NOUVEL, num=130)]}))
    os.utime(agents_dir / "perso.yaml", ns=(1, 2_000_000_000_000_000_000))
    assert nb_agents(client, h_api) == 26
    ecrire(agents_dir, "perso.yaml", "agents: [une faute de frappe : :")
    os.utime(agents_dir / "perso.yaml", ns=(1, 3_000_000_000_000_000_000))
    assert nb_agents(client, h_api) == 26                      # l'agent du client n'a pas disparu
    assert any("dernière version valide" in w for w in client.get("/health").json()["avertissements"])


def test_registre_vide_par_erreur_ne_vide_pas_les_agents(client, h_api, agents_dir):
    ecrire(agents_dir, "registre.yaml", "agents: []\n")
    os.utime(agents_dir / "registre.yaml", ns=(1, 2_000_000_000_000_000_000))
    assert nb_agents(client, h_api) == 25


def test_registre_casse_en_service_garde_la_version_precedente(client, h_api, agents_dir):
    ecrire(agents_dir, "registre.yaml", "agents: [cassé")
    os.utime(agents_dir / "registre.yaml", ns=(1, 2_000_000_000_000_000_000))
    assert nb_agents(client, h_api) == 25


@pytest.mark.parametrize("contenu", ["- id: a\n", "texte", b"\xff\xfe\x81", "agents: [cassé"])
def test_registre_casse_au_demarrage_ne_plante_pas_le_demarrage(monkeypatch, agents_dir, contenu):
    ecrire(agents_dir, "registre.yaml", contenu)
    app = charger_app(monkeypatch, agents_dir)            # ne doit pas lever d'exception
    with TestClient(app.app) as tc:
        h = tc.get("/health").json()
        assert h["agents"] == 0 and h["avertissements"]


def test_cerveaux_yaml_de_mauvais_type_ne_casse_pas_les_requetes(client, h_api, agents_dir, faux):
    ecrire(agents_dir, "cerveaux.yaml", "- une liste\n- au lieu d'un dictionnaire\n")
    os.utime(agents_dir / "cerveaux.yaml", ns=(1, 2_000_000_000_000_000_000))
    r = client.post("/v1/chat/completions", headers=h_api, json={
        "model": "pk-12-secretariat", "messages": [{"role": "user", "content": "salut"}]})
    assert r.status_code == 200


def test_doublons_id_et_numero_dans_perso_sont_ignores_sans_toucher_aux_25(client, h_api, agents_dir):
    ecrire(agents_dir, "perso.yaml", yaml.safe_dump({"agents": [
        dict(NOUVEL, id="commercial", num=140),      # id déjà pris
        dict(NOUVEL, id="autre", num=2),             # numéro déjà pris
        dict(NOUVEL, id="auto", num=141),            # id réservé
        dict(NOUVEL, id="ok-one", num=142),
    ]}))
    os.utime(agents_dir / "perso.yaml", ns=(1, 2_000_000_000_000_000_000))
    ids = [m["id"] for m in client.get("/v1/models", headers=h_api).json()["data"]]
    assert "pk-02-commercial" in ids and "pk-142-ok-one" in ids and len(ids) == 2 + 26
    assert len(client.get("/health").json()["avertissements"]) == 3


# ---------------------------------------------------------------- POST /admin/agents
def poster(client, h, corps, **kw):
    return client.post("/admin/agents", headers=h, content=corps if isinstance(corps, (str, bytes)) else yaml.safe_dump(corps), **kw)


def test_ajout_valide(client, h_api, h_admin, agents_dir):
    r = poster(client, h_admin, NOUVEL)
    assert r.status_code == 201, r.text
    assert r.json()["modele"] == "pk-101-garagiste"
    assert nb_agents(client, h_api) == 26
    sauve = yaml.safe_load((agents_dir / "perso.yaml").read_text(encoding="utf-8"))
    assert [a["id"] for a in sauve["agents"]] == ["garagiste"]
    # le nouvel agent est atteignable par pk-auto et en direct
    assert client.post("/route", headers=h_api, json={"texte": "il faut faire la vidange et changer les pneus"}).json()["agent"] == "garagiste"


def test_ajout_en_json_ou_bloc_yaml_avec_balises(client, h_admin):
    assert poster(client, h_admin, '{"id":"j1","nom":"J","cerveau":"pk-rapide","description":"d","consignes":"c"}').status_code == 201
    bloc = "```yaml\n" + yaml.safe_dump(dict(NOUVEL, id="y1")) + "```"
    assert poster(client, h_admin, bloc).status_code == 201


def test_ajout_numerotation_automatique_sans_trou_ni_doublon(client, h_admin):
    nums = []
    for i in range(3):
        nums.append(poster(client, h_admin, dict(NOUVEL, id=f"a{i}")).json()["modele"])
    assert nums == ["pk-101-a0", "pk-102-a1", "pk-103-a2"]


@pytest.mark.parametrize("modif,mot", [
    ({"id": "commercial"}, "déjà utilisé"),
    ({"id": "AUTO"}, "invalide"), ({"id": "auto"}, "réservé"), ({"id": "equipe"}, "réservé"),
    ({"id": "a b"}, "invalide"), ({"id": "abc\n"}, "invalide"), ({"id": "../etc"}, "invalide"), ({"id": 12345}, "id"),
    ({"num": 2}, "déjà utilisé"), ({"num": 0}, "num"), ({"num": 1000}, "num"), ({"num": "abc"}, "num"),
    ({"num": 3.7}, "num"), ({"num": True}, "num"),
    ({"cerveau": "gpt-4"}, "inconnu"), ({"cerveau": ["pk-rapide"]}, "cerveau"),
    ({"famille": "inconnue"}, "famille"), ({"famille": ["x"]}, "famille"),
    ({"temperature": 5}, "temperature"), ({"temperature": "chaud"}, "temperature"), ({"temperature": True}, "temperature"),
    ({"mots_cles": "a, b"}, "mots_cles"), ({"mots_cles": [["x"]]}, "mots_cles"),
    ({"nom": ["liste"]}, "nom"), ({"consignes": 123}, "consignes"), ({"description": {"a": 1}}, "description"),
    ({"emoji": ["x"]}, "emoji"), ({"nom": ""}, "nom"),
])
def test_ajout_refuse_proprement(client, h_api, h_admin, agents_dir, modif, mot):
    avant = (agents_dir / "perso.yaml").read_bytes()
    r = poster(client, h_admin, dict(NOUVEL, **modif))
    assert r.status_code == 422, r.text
    assert mot in r.json()["error"]["message"]
    assert (agents_dir / "perso.yaml").read_bytes() == avant      # rien n'a été écrit
    assert nb_agents(client, h_api) == 25
    assert client.post("/route", headers=h_api, json={"texte": "devis"}).status_code == 200  # le routage tient


def test_ajout_champ_manquant(client, h_admin):
    r = poster(client, h_admin, {"id": "sans-reste"})
    assert r.status_code == 422 and "manquant" in r.json()["error"]["message"]


@pytest.mark.parametrize("corps", ["", "   ", "[]", "null", "42", "juste du texte : : :", "{\"agents\": null}", b"\xff\xfe\x81"])
def test_ajout_corps_invalide_est_une_erreur_400_ou_422_jamais_500(client, h_admin, corps):
    r = poster(client, h_admin, corps)
    assert r.status_code in (400, 422), r.text
    assert r.json()["error"]["message"]


def test_ajout_corps_trop_gros_refuse(client, h_admin):
    r = poster(client, h_admin, "x" * 200_000)
    assert r.status_code == 413


def test_ajout_plusieurs_agents_a_la_fois_est_signale(client, h_admin, h_api):
    r = poster(client, h_admin, {"agents": [dict(NOUVEL, id="p1"), dict(NOUVEL, id="p2")]})
    assert r.status_code == 422 and "un seul" in r.json()["error"]["message"].lower()
    assert nb_agents(client, h_api) == 25


def test_ajout_quand_perso_est_illisible_refuse_sans_ecraser(client, h_admin, agents_dir):
    ecrire(agents_dir, "perso.yaml", "agents: [le fichier de Will, à moitié cassé : :")
    avant = (agents_dir / "perso.yaml").read_bytes()
    r = poster(client, h_admin, NOUVEL)
    assert r.status_code == 409 and "perso.yaml" in r.json()["error"]["message"]
    assert (agents_dir / "perso.yaml").read_bytes() == avant


def test_ecriture_atomique_si_le_remplacement_echoue(client, h_admin, agents_dir, monkeypatch):
    avant = (agents_dir / "perso.yaml").read_bytes()

    def boum(*a, **k):
        raise OSError("disque plein")
    monkeypatch.setattr(os, "replace", boum)
    r = poster(client, h_admin, NOUVEL)
    assert r.status_code >= 500
    assert (agents_dir / "perso.yaml").read_bytes() == avant
    assert not [p for p in agents_dir.iterdir() if p.name.startswith(".perso-")]   # pas de fichier temporaire oublié


def test_ecriture_garde_un_fichier_valide_apres_chaque_ajout(client, h_admin, agents_dir):
    for i in range(4):
        assert poster(client, h_admin, dict(NOUVEL, id=f"ag{i}")).status_code == 201
        assert len(yaml.safe_load((agents_dir / "perso.yaml").read_text(encoding="utf-8"))["agents"]) == i + 1
    assert not [p for p in agents_dir.iterdir() if p.name.startswith(".perso-")]


def test_retrait_d_un_agent_perso_et_refus_pour_un_agent_du_registre(client, h_admin, h_api):
    poster(client, h_admin, NOUVEL)
    assert client.delete("/admin/agents/garagiste", headers=h_admin).status_code == 200
    assert nb_agents(client, h_api) == 25
    r = client.delete("/admin/agents/commercial", headers=h_admin)
    assert r.status_code == 404 and nb_agents(client, h_api) == 25


def test_retrait_quand_perso_illisible_ne_plante_pas(client, h_admin, agents_dir):
    ecrire(agents_dir, "perso.yaml", "agents: [cassé : :")
    r = client.delete("/admin/agents/x", headers=h_admin)
    assert r.status_code == 409


def test_un_agent_perso_mal_forme_deja_present_ne_casse_pas_pk_auto(client, h_api, agents_dir):
    """Ancien fichier écrit à la main avec un nom qui n'est pas du texte."""
    ecrire(agents_dir, "perso.yaml", yaml.safe_dump({"agents": [dict(NOUVEL, num=150, nom=["x"], consignes=12)]}))
    os.utime(agents_dir / "perso.yaml", ns=(1, 2_000_000_000_000_000_000))
    assert client.post("/route", headers=h_api, json={"texte": "un devis"}).status_code == 200
    assert nb_agents(client, h_api) == 25


# ---------------------------------------------------------------- aucun agent chargé
@pytest.fixture
def client_sans_agent(monkeypatch, agents_dir, faux):
    """Registre illisible dès le démarrage : 0 agent."""
    ecrire(agents_dir, "registre.yaml", "agents: [cassé")
    app = charger_app(monkeypatch, agents_dir)
    with TestClient(app.app, raise_server_exceptions=False) as tc:
        tc.app.state.client = __import__("httpx").AsyncClient(transport=__import__("httpx").MockTransport(faux))
        tc.faux = faux
        yield tc


@pytest.mark.parametrize("modele", ["pk-auto", "auto", "pk-equipe", "pk-02-commercial"])
def test_sans_agent_chargé_reponse_claire_503(client_sans_agent, h_api, modele):
    r = client_sans_agent.post("/v1/chat/completions", headers=h_api, json={
        "model": modele, "messages": [{"role": "user", "content": "Fais un devis"}]})
    assert r.status_code == 503, r.text
    e = r.json()["error"]
    assert "aucun agent disponible" in e["message"].lower() and "/health" in e["message"]
    assert e["code"] == 503 and e["type"] == "aucun_agent"
    assert client_sans_agent.faux.appels == []          # on n'a appelé aucun cerveau pour rien


def test_sans_agent_chargé_route_et_flux_aussi(client_sans_agent, h_api):
    assert client_sans_agent.post("/route", headers=h_api, json={"texte": "devis"}).status_code == 503
    r = client_sans_agent.post("/v1/chat/completions", headers=h_api, json={
        "model": "pk-auto", "stream": True, "messages": [{"role": "user", "content": "salut"}]})
    assert r.status_code == 503


def test_sans_agent_chargé_health_dit_pourquoi(client_sans_agent):
    h = client_sans_agent.get("/health").json()
    assert h["ok"] is False and h["agents"] == 0 and h["avertissements"]
