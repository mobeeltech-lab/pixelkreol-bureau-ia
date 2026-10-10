"""Banc d'essai de l'orchestrateur.

Règles : aucune vraie clé, aucun vrai fournisseur. LiteLLM est remplacé par un faux serveur
(`FauxLiteLLM`, branché sur httpx.MockTransport) et toute tentative de connexion réseau
fait échouer le test.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import sys
import uuid
from pathlib import Path

import httpx
import pytest

RACINE = Path(__file__).resolve().parent.parent
APP_PY = RACINE / "orchestrateur" / "app.py"
CLE_API = "cle-api-de-test"
CLE_ADMIN = "cle-admin-de-test"
CLE_LITELLM = "cle-litellm-de-test"


@pytest.fixture(autouse=True)
def pas_de_reseau(monkeypatch):
    """Filet de sécurité : un test ne doit JAMAIS ouvrir de connexion réseau."""
    def interdit(self, *a, **k):
        raise AssertionError("Connexion réseau interdite dans les tests")
    monkeypatch.setattr(socket.socket, "connect", interdit)


def charger_app(monkeypatch, dossier_agents: Path, **env):
    """Importe orchestrateur/app.py à neuf, avec un dossier d'agents et des variables isolés."""
    for k in list(__import__("os").environ):
        if k.endswith("_API_KEY") or k in ("DATABASE_URL", "PK_API_KEY", "PK_ADMIN_KEY", "LITELLM_MASTER_KEY"):
            monkeypatch.delenv(k, raising=False)
    base = {"PK_AGENTS_DIR": str(dossier_agents), "LITELLM_MASTER_KEY": CLE_LITELLM,
            "PK_API_KEY": CLE_API, "PK_ADMIN_KEY": CLE_ADMIN, "LITELLM_URL": "http://faux-litellm:4000",
            "PK_ROUTAGE_IA": "true", "PK_ANNONCE_AGENT": "false", "PK_LOG_LEVEL": "WARNING"}
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, str(v))
    nom = f"pk_app_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(nom, APP_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nom] = mod
    spec.loader.exec_module(mod)
    return mod


class FauxLiteLLM:
    """Faux LiteLLM : on lui dit quoi répondre, il note tous les appels reçus."""

    def __init__(self):
        self.appels: list[dict] = []        # corps JSON reçus
        self.scenarios: dict[str, list] = {}  # model -> liste de réponses à servir dans l'ordre
        self.par_defaut = None

    # --- fabrication de réponses -----------------------------------------
    @staticmethod
    def ok(texte="Réponse de test", usage=None):
        corps = {"id": "chatcmpl-test", "object": "chat.completion", "created": 1, "model": "x",
                 "choices": [{"index": 0, "message": {"role": "assistant", "content": texte},
                              "finish_reason": "stop"}],
                 "usage": usage or {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}}
        return httpx.Response(200, json=corps)

    @staticmethod
    def statut(code, texte="erreur simulée"):
        return httpx.Response(code, json={"error": {"message": texte}})

    @staticmethod
    def flux(morceaux, coupe=False):
        class _S(httpx.AsyncByteStream):
            async def __aiter__(self_inner):
                for m in morceaux:
                    yield m
                if coupe:
                    raise httpx.ReadError("connexion coupée")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_S())

    @staticmethod
    def sse(texte):
        o = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "x",
             "choices": [{"index": 0, "delta": {"content": texte}, "finish_reason": None}]}
        return f"data: {json.dumps(o)}\n\n".encode()

    # --- le « serveur » ----------------------------------------------------
    def __call__(self, requete: httpx.Request):
        if requete.url.path.endswith("/health/liveliness"):
            return httpx.Response(200, json={"ok": True})
        corps = json.loads(requete.content)
        corps["_auth"] = requete.headers.get("authorization", "")
        self.appels.append(corps)
        file = self.scenarios.get(corps["model"])
        if file:
            rep = file.pop(0) if len(file) > 1 else file[0]
        else:
            rep = self.par_defaut
        if callable(rep):
            rep = rep(corps)
        if isinstance(rep, Exception):
            raise rep
        if rep is None:
            rep = self.ok()
        return rep

    def modeles_appeles(self):
        return [a["model"] for a in self.appels]


@pytest.fixture
def agents_dir(tmp_path):
    dest = tmp_path / "agents"
    shutil.copytree(RACINE / "agents", dest, ignore=shutil.ignore_patterns("*.md"))
    return dest


@pytest.fixture
def faux():
    return FauxLiteLLM()


@pytest.fixture
def appli(monkeypatch, agents_dir):
    return charger_app(monkeypatch, agents_dir)


@pytest.fixture
def client(appli, faux):
    from fastapi.testclient import TestClient
    with TestClient(appli.app, raise_server_exceptions=False) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        tc.faux, tc.appli = faux, appli
        yield tc


@pytest.fixture
def h_api():
    return {"Authorization": f"Bearer {CLE_API}"}


@pytest.fixture
def h_admin():
    return {"Authorization": f"Bearer {CLE_ADMIN}"}
