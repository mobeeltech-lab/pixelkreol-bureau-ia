"""MCP : la validation par Will exige une clé d'administration DÉDIÉE (≥ 32 caractères, ≠ LITELLM_MASTER_KEY, ≠ PK_API_KEY). Sinon la page et les routes
valider / refuser / classer refusent de répondre, avec un message clair dans /health et dans les journaux."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestrateur"))
from conftest import CLE_API, CLE_LITELLM, RACINE, FauxLiteLLM  # noqa: E402
import mcp_client as M  # noqa: E402
from test_mcp import AGENT, CLE_ADMIN_MCP, MODELE, brancher, monter, reponse_outil, OK  # noqa: E402

LONGUE = "L" * 40


def demande(c, cle_api=CLE_API):
    return c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {cle_api}"}, json={"model": MODELE, "messages": [{"role": "user", "content": "Cherche"}]})
P = "/pk-validation"


# =============================================================== la règle elle-même
@pytest.mark.parametrize("admin,litellm,api,attendu", [
    ("", "ll", "api", "absente"),
    ("x" * 31, "ll", "api", "31 caractères"),
    ("x" * 5, "", "", "5 caractères"),
    (LONGUE, LONGUE, "api", "LITELLM_MASTER_KEY"),
    (LONGUE, "ll", LONGUE, "PK_API_KEY"),
])
def test_cles_refusees_avec_une_raison_claire(admin, litellm, api, attendu):
    raison = M.verifier_cle_admin(admin, litellm, api)
    assert raison and attendu in raison and "validation MCP est désactivée" in raison
    assert admin not in raison or admin == ""                                       # jamais la clé dans le message


@pytest.mark.parametrize("admin", ["x" * 32, "x" * 33, LONGUE, "a1B2" * 20])
def test_cles_acceptees(admin):
    assert M.verifier_cle_admin(admin, "litellm-autre", "api-autre") is None


# =============================================================== comportement de l'orchestrateur
CAS = {
    "absente": {"PK_ADMIN_KEY": ""},
    "courte": {"PK_ADMIN_KEY": "x" * 31},
    "egale_litellm": {"PK_ADMIN_KEY": LONGUE, "LITELLM_MASTER_KEY": LONGUE},
    "egale_api": {"PK_ADMIN_KEY": LONGUE, "PK_API_KEY": LONGUE},
}


def monter_avec(monkeypatch, tmp_path, agents_dir, faux, env):
    app = monter(monkeypatch, tmp_path, agents_dir, **env)
    c = TestClient(app.app, raise_server_exceptions=False)
    c.__enter__()
    c.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
    brancher(app, c)
    c.appli, c.faux = app, faux
    return c


@pytest.mark.parametrize("nom", list(CAS))
def test_validation_refusee_page_routes_sante_journal(nom, monkeypatch, tmp_path, agents_dir, caplog):
    faux = FauxLiteLLM()
    caplog.set_level(logging.ERROR)
    c = monter_avec(monkeypatch, tmp_path, agents_dir, faux, CAS[nom])
    try:
        cle = CAS[nom]["PK_ADMIN_KEY"] or CLE_API
        sante = c.get("/health").json()["mcp"]
        assert sante["actif"] is True and sante["validation"]["disponible"] is False and "validation MCP est désactivée" in sante["validation"]["raison"]
        assert LONGUE not in str(sante)
        assert any("validation MCP est désactivée" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
        # page et routes : 503 avec la raison, même avec la « bonne » clé
        r = c.get(f"{P}/")
        assert r.status_code == 503 and "validation MCP est désactivée" in r.json()["error"]["message"]
        assert c.post(f"{P}/connexion", data={"cle": cle}).status_code == 503
        for verbe in ("valider", "refuser", "classer"):
            assert c.post(f"/admin/mcp/{verbe}/0123456789", headers={"Authorization": f"Bearer {cle}"}).status_code == 503
            assert c.post(f"{P}/{verbe}/0123456789", data={"jeton": "x"}).status_code == 503
        # aucun outil d'écriture proposé : l'agent ne peut pas mettre en attente une action que personne ne pourrait valider
        cle_api = CAS[nom].get("PK_API_KEY", CLE_API)
        demande(c, cle_api)
        noms = [t["function"]["name"] for t in faux.appels[0]["tools"]]
        assert "mcp__courrier__create_draft" not in noms and "mcp__pages__fetch" in noms and "mcp__courrier__search_threads" in noms        # la lecture continue
        faux.scenarios["pk-long"] = [reponse_outil([("mcp__courrier__create_draft", {"sujet": "S"})]), OK("ok")]
        demande(c, cle_api)
        tool = [m for m in faux.appels[-1]["messages"] if m["role"] == "tool"][0]["content"]
        assert "validation de Will, actuellement indisponible" in tool
        assert c.appli._faux["courrier"].appels == [] and c.appli.MCPC.base.actions() == []
        # les routes de lecture restent accessibles à la clé effective
        assert c.get("/admin/mcp/etat", headers={"Authorization": f"Bearer {cle}"}).json()["validation"]["disponible"] is False
    finally:
        c.__exit__(None, None, None)


def test_une_bonne_cle_dediee_active_la_validation(monkeypatch, tmp_path, agents_dir):
    faux = FauxLiteLLM()
    c = monter_avec(monkeypatch, tmp_path, agents_dir, faux, {"PK_ADMIN_KEY": "x" * 32})
    try:
        assert c.get("/health").json()["mcp"]["validation"] == {"disponible": True, "raison": ""}
        assert c.get(f"{P}/").status_code == 200
        demande(c)
        assert "mcp__courrier__create_draft" in [t["function"]["name"] for t in faux.appels[0]["tools"]]
    finally:
        c.__exit__(None, None, None)


def test_une_action_deja_en_attente_ne_peut_pas_etre_validee_avec_une_cle_inadaptee(monkeypatch, tmp_path, agents_dir):
    faux = FauxLiteLLM()
    c = monter_avec(monkeypatch, tmp_path, agents_dir, faux, {"PK_ADMIN_KEY": "x" * 32})
    try:
        faux.scenarios["pk-long"] = [reponse_outil([("mcp__courrier__create_draft", {"sujet": "S"})]), OK("ok")]
        demande(c)
        [a] = c.appli.MCPC.base.actions("attente")
        c.appli.MCPC.validation_pb = "clé inadaptée (simulation)"                        # défense en profondeur dans le module lui-même
        import asyncio
        assert asyncio.run(c.appli.MCPC.valider(a["id"]))["code"] == "indisponible"
        assert asyncio.run(c.appli.MCPC.refuser(a["id"]))["code"] == "indisponible"
        assert c.appli._faux["courrier"].appels == []
    finally:
        c.__exit__(None, None, None)


def test_sans_mcp_actif_rien_de_cela_ne_se_voit(monkeypatch, tmp_path, agents_dir):
    faux = FauxLiteLLM()
    c = monter_avec(monkeypatch, tmp_path, agents_dir, faux, {"PK_ADMIN_KEY": "court", "PK_MCP_ACTIF": "0"})
    try:
        assert "mcp" not in c.get("/health").json() and c.get(f"{P}/").status_code == 404
    finally:
        c.__exit__(None, None, None)


# =============================================================== le compose
def test_compose_mcp_ne_retombe_plus_sur_litellm_master_key():
    texte = (RACINE / "docker-compose.mcp.yml").read_text(encoding="utf-8")
    env = yaml.safe_load(texte)["services"]["pk_orchestrateur"]["environment"]
    assert env["PK_MCP_ACTIF"] == "1"
    assert env["PK_ADMIN_KEY"].startswith("${PK_ADMIN_KEY:?") and "LITELLM_MASTER_KEY" not in env["PK_ADMIN_KEY"].split("}")[0].replace("différente de LITELLM_MASTER_KEY", "")
    assert ":-${LITELLM_MASTER_KEY}" not in texte
