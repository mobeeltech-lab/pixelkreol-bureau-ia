"""Outil « documents » branché dans l'orchestrateur : appel d'outils, repli, désactivé par défaut, PixelKréol inchangé."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
pytest.importorskip("reportlab")
pytest.importorskip("docx")
pytest.importorskip("openpyxl")
pytest.importorskip("pypdf")

import fabrique_nas as fab  # noqa: E402
from conftest import CLE_ADMIN, CLE_API, RACINE, FauxLiteLLM, charger_app  # noqa: E402

OK = FauxLiteLLM.ok
AGENTS_ABI = RACINE / "instances" / "abi" / "agents"
ENV_ABI = {"PK_MARQUE": "ABI Holding", "PK_LIEU": "en Corse", "PK_FUSEAU": "Europe/Paris", "PK_PATRON": "la direction d'ABI Holding",
           "PK_PROPRIETAIRE": "abi"}
ACTE = "Oliviers/acquereurs/FICTIF-DURAND/acte-vefa-A02.pdf"
H = {"Authorization": f"Bearer {CLE_API}"}


@pytest.fixture(scope="module")
def modele(tmp_path_factory):
    base = tmp_path_factory.mktemp("modele-nas-orch")
    fab.fabriquer(base / "nas", base / "dehors")
    return base


@pytest.fixture
def nas(tmp_path, modele):
    return fab.copie_fraiche(modele, tmp_path)


def reponse_outil(nom, args, ident="call_1"):
    return httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 1, "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": ident, "type": "function", "function": {"name": nom, "arguments": json.dumps(args)}}]}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}})


def monter(monkeypatch, tmp_path, nas, faux, **env):
    agents = tmp_path / "agents"
    import shutil
    shutil.copytree(AGENTS_ABI, agents)
    base = dict(ENV_ABI, PK_NAS_ACTIF="1", PK_NAS_CHEMIN=str(nas), PK_NAS_INDEX=str(tmp_path / "index.sqlite"),
                PK_NAS_TAILLE_MAX_MO="1", PK_NAS_CARACTERES_MAX="3000", PK_NAS_DELAI="10")
    base.update(env)
    app = charger_app(monkeypatch, agents, **base)
    return app


@pytest.fixture
def faux():
    return FauxLiteLLM()


@pytest.fixture
def client_nas(monkeypatch, tmp_path, nas, faux):
    app = monter(monkeypatch, tmp_path, nas, faux)
    with TestClient(app.app, raise_server_exceptions=False) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        assert app.DOCS is not None
        app.DOCS.cfg.pause = 0
        app.DOCS.indexer()
        tc.appli, tc.faux, tc.nas = app, faux, nas
        yield tc


def demande(tc, texte="Retrouve l'acte de vente du lot A02", modele="pk-09-archiviste", stream=False, **extra):
    return tc.post("/v1/chat/completions", headers={**H, **extra.pop("entetes", {})}, json={
        "model": modele, "stream": stream, "messages": [{"role": "user", "content": texte}], **extra})


def journal(tc):
    with tc.appli.DOCS._base() as con:
        return [dict(r) for r in con.execute("SELECT * FROM journal ORDER BY id")]


# =============================================================== le modèle appelle l'outil
def test_archiviste_cherche_puis_repond_avec_le_chemin_exact(client_nas):
    f = client_nas.faux
    f.scenarios["pk-long"] = [reponse_outil("rechercher_documents", {"texte": "acte vente A02"}),
                              OK(f"L'acte est ici : {ACTE}")]
    r = demande(client_nas)
    assert r.status_code == 200
    assert ACTE in r.json()["choices"][0]["message"]["content"] and "Non vérifié" not in r.json()["choices"][0]["message"]["content"]
    assert len(f.appels) == 2
    premier, second = f.appels
    assert [t["function"]["name"] for t in premier["tools"]] == ["rechercher_documents", "lire_document"]
    assert premier["tool_choice"] == "auto"
    assert "Outil « documents »" in premier["messages"][0]["content"]
    tool = [m for m in second["messages"] if m["role"] == "tool"][0]
    assert tool["tool_call_id"] == "call_1" and ACTE in tool["content"]
    assert any(m.get("tool_calls") for m in second["messages"] if m["role"] == "assistant")
    assert r.json()["usage"] == {"prompt_tokens": 10, "completion_tokens": 14, "total_tokens": 24}      # usage additionné
    assert r.headers["x-pk-agent"] == "archiviste"


def test_lecture_d_un_extrait_seulement_jamais_le_document_entier(client_nas):
    (client_nas.nas / "long.txt").write_text("DONNÉES FICTIVES — TEST " + "alpha " * 5000 + "FIN-UNIQUE", encoding="utf-8")
    client_nas.appli.DOCS.indexer()
    f = client_nas.faux
    f.scenarios["pk-long"] = [reponse_outil("lire_document", {"chemin": "long.txt"}), OK("Voici le début de long.txt")]
    demande(client_nas, "Lis long.txt")
    tool = [m for m in f.appels[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert len(tool) <= 3000 + 1500 and "FIN-UNIQUE" not in tool and "suite_disponible" in tool


def test_le_journal_dit_qui_a_demande_sans_le_contenu(client_nas):
    f = client_nas.faux
    f.scenarios["pk-long"] = [reponse_outil("lire_document", {"chemin": ACTE}), OK("ok")]
    demande(client_nas, entetes={"X-OpenWebUI-User-Name": "Marie Test", "X-OpenWebUI-User-Id": "u-42"})
    ligne = [l for l in journal(client_nas) if l["action"] == "lecture"][0]
    assert ligne["demandeur"] == "Marie Test (u-42)" and ligne["agent"] == "archiviste" and ligne["cible"] == ACTE
    assert "232 000" not in json.dumps(journal(client_nas), ensure_ascii=False)
    f.scenarios["pk-long"] = [reponse_outil("lire_document", {"chemin": ACTE}), OK("ok")]
    demande(client_nas, user="n8n-flux-relances")
    assert [l for l in journal(client_nas) if l["action"] == "lecture"][-1]["demandeur"] == "n8n-flux-relances"


def test_le_modele_qui_demande_d_ecrire_ou_de_supprimer_est_refuse(client_nas):
    f = client_nas.faux
    f.scenarios["pk-long"] = [reponse_outil("supprimer_document", {"chemin": ACTE}), OK("Je ne peux pas supprimer.")]
    avant = fab.instantane(client_nas.nas)
    r = demande(client_nas, "Supprime l'acte du lot A02")
    assert r.status_code == 200
    tool = [m for m in f.appels[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "LECTURE SEULE" in tool
    assert fab.instantane(client_nas.nas) == avant
    assert any(l["action"] == "refus" for l in journal(client_nas))


def test_boucle_d_outils_bornee(client_nas):
    f = client_nas.faux
    f.scenarios["pk-long"] = [reponse_outil("rechercher_documents", {"texte": "acte"})] * 4 + [OK("Réponse finale")]
    r = demande(client_nas)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "Réponse finale"
    assert len(f.appels) == 5                                  # 4 tours d'outils au plus, puis un dernier appel pour conclure
    assert f.appels[-1]["tool_choice"] == "none"


def test_les_agents_sans_outil_n_en_recoivent_pas(client_nas):
    for modele in ("pk-01-orchestrateur", "pk-05-commercial"):
        client_nas.faux.appels.clear()
        demande(client_nas, "bonjour", modele)
        assert "tools" not in client_nas.faux.appels[0] and "Outil « documents »" not in client_nas.faux.appels[0]["messages"][0]["content"]


def test_les_outils_du_client_ne_sont_pas_ecrases(client_nas):
    outil_client = [{"type": "function", "function": {"name": "mon_outil", "parameters": {"type": "object", "properties": {}}}}]
    demande(client_nas, "bonjour", tools=outil_client)
    assert client_nas.faux.appels[0]["tools"] == outil_client


def test_flux_avec_outils(client_nas):
    f = client_nas.faux
    f.scenarios["pk-long"] = [reponse_outil("rechercher_documents", {"texte": "acte"}), OK(f"Trouvé : {ACTE}")]
    r = demande(client_nas, stream=True)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    texte = "".join(json.loads(l[6:])["choices"][0]["delta"].get("content") or "" for l in r.text.splitlines() if l.startswith("data: {"))
    assert ACTE in texte and r.text.rstrip().endswith("data: [DONE]")


def test_pk_equipe_utilise_aussi_l_outil(client_nas):
    f = client_nas.faux

    def repond(corps):
        user = corps["messages"][-1]["content"]
        if "PLANIFICATION_JSON" in user:
            return OK(json.dumps({"taches": [{"agent": "archiviste", "consigne": "retrouve l'acte A02"}]}))
        if "contributions de l'équipe" in user:
            return OK("Synthèse")
        if any(m["role"] == "tool" for m in corps["messages"]):
            return OK(f"Trouvé {ACTE}")
        return reponse_outil("rechercher_documents", {"texte": "acte A02"})
    f.par_defaut = repond
    r = client_nas.post("/v1/chat/completions", headers=H, json={"model": "pk-equipe", "messages": [{"role": "user", "content": "Retrouve l'acte"}]},
                        )
    assert r.status_code == 200 and ACTE in r.json()["choices"][0]["message"]["content"]
    assert any(l["action"] == "recherche" for l in journal(client_nas))


# =============================================================== ne jamais inventer un document
def test_nom_de_document_invente_est_signale(client_nas):
    client_nas.faux.par_defaut = OK("Le contrat est dans Oliviers/juridique/contrat-imaginaire-2026.pdf et dans acte-vefa-A02.pdf")
    texte = demande(client_nas).json()["choices"][0]["message"]["content"]
    assert "Non vérifié dans le NAS" in texte and "contrat-imaginaire-2026.pdf" in texte
    assert texte.split("Non vérifié dans le NAS")[1].count("acte-vefa-A02.pdf") == 0       # le vrai fichier n'est pas accusé


def test_nom_en_gras_ou_backticks_n_est_pas_accuse_a_tort(client_nas):
    client_nas.faux.par_defaut = OK("Voir **acte-vefa-A02.pdf** et `acte-vefa-A02.pdf`, ainsi que **`acte-vefa-A02.pdf`**.")
    texte = demande(client_nas).json()["choices"][0]["message"]["content"]
    assert "Non vérifié" not in texte
    client_nas.faux.par_defaut = OK("Voir **contrat-imaginaire-2026.pdf** et `acte-vefa-A02.pdf`.")
    texte = demande(client_nas).json()["choices"][0]["message"]["content"]
    assert "Non vérifié dans le NAS" in texte and texte.split("Non vérifié dans le NAS")[1].count("contrat-imaginaire-2026.pdf") == 1
    assert "**contrat" not in texte.split("Non vérifié dans le NAS")[1] and texte.split("Non vérifié dans le NAS")[1].count("acte-vefa-A02.pdf") == 0


# =============================================================== repli si le modèle ne sait pas appeler d'outils
def test_repli_automatique_quand_le_cerveau_refuse_les_outils(client_nas):
    f = client_nas.faux
    f.par_defaut = lambda corps: FauxLiteLLM.statut(400, "tools not supported") if "tools" in corps else OK(f"D'après l'index : {ACTE}")
    r = demande(client_nas, "Retrouve l'acte de vente du lot A02 de FICTIF-DURAND")
    assert r.status_code == 200 and ACTE in r.json()["choices"][0]["message"]["content"]
    assert len(f.appels) == 2 and "tools" in f.appels[0] and "tools" not in f.appels[1]
    systeme = f.appels[1]["messages"][0]["content"]
    assert "Documents trouvés dans le NAS" in systeme and ACTE in systeme and "232 000,00" in systeme
    assert any(l["action"] == "recherche" for l in journal(client_nas))


def test_mode_repli_force_n_envoie_jamais_d_outils(monkeypatch, tmp_path, nas, faux):
    app = monter(monkeypatch, tmp_path, nas, faux, PK_NAS_MODE="repli")
    with TestClient(app.app) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        app.DOCS.indexer()
        faux.par_defaut = OK("réponse")
        demande(tc, "acte de vente A02 achèvement")
        assert len(faux.appels) == 1 and "tools" not in faux.appels[0]
        assert ACTE in faux.appels[0]["messages"][0]["content"]


def test_repli_sans_resultat_dit_introuvable(client_nas):
    f = client_nas.faux
    f.par_defaut = lambda corps: FauxLiteLLM.statut(422, "tools") if "tools" in corps else OK("Introuvable dans le NAS.")
    demande(client_nas, "licorne-introuvable-xyz")
    assert "Aucun document trouvé dans le NAS" in f.appels[1]["messages"][0]["content"]


def test_autres_erreurs_ne_declenchent_pas_le_repli(client_nas):
    client_nas.faux.par_defaut = FauxLiteLLM.statut(401, "clé invalide")
    r = demande(client_nas)
    assert r.status_code == 401 and len(client_nas.faux.appels) == 1


# =============================================================== administration de l'index
def test_admin_nas_etat_et_reindexation(client_nas):
    assert client_nas.get("/admin/nas").status_code == 401
    assert client_nas.get("/admin/nas", headers={"Authorization": f"Bearer {CLE_API}"}).status_code == 401      # clé API ≠ clé admin
    h = {"Authorization": f"Bearer {CLE_ADMIN}"}
    etat = client_nas.get("/admin/nas", headers=h).json()
    assert etat["actif"] and etat["fichiers_indexes"] == 10 and etat["fts5"]
    (client_nas.nas / "Nouveau.txt").write_text("DONNÉES FICTIVES — TEST nouveau", encoding="utf-8")
    assert client_nas.post("/admin/nas/reindexer").status_code == 401
    assert client_nas.post("/admin/nas/reindexer", headers=h).status_code == 202
    for _ in range(50):
        if client_nas.get("/admin/nas", headers=h).json()["fichiers_indexes"] == 11:
            break
        time.sleep(0.1)
    assert client_nas.get("/admin/nas", headers=h).json()["fichiers_indexes"] == 11


def test_sante_signale_le_nas_et_son_absence(client_nas):
    assert client_nas.get("/health").json()["nas"] == {"actif": True, "disponible": True}


def test_demarrage_avec_un_chemin_invalide_laisse_l_outil_eteint_sans_panne(monkeypatch, tmp_path, nas, faux):
    app = monter(monkeypatch, tmp_path, nas, faux, PK_NAS_CHEMIN="chemin/relatif")
    with TestClient(app.app) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        assert app.DOCS is None and tc.get("/health").json()["nas"] == {"actif": False, "disponible": False}
        assert demande(tc).status_code == 200 and "tools" not in faux.appels[0]
        assert tc.get("/admin/nas", headers={"Authorization": f"Bearer {CLE_ADMIN}"}).status_code == 404


def test_index_non_inscriptible_laisse_l_outil_eteint_sans_panne(monkeypatch, tmp_path, nas, faux):
    app = monter(monkeypatch, tmp_path, nas, faux, PK_NAS_INDEX=str(tmp_path / "dossier-absent" / "index.sqlite"))
    with TestClient(app.app) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        assert app.DOCS is None and demande(tc).status_code == 200


def test_nas_demonte_pendant_l_usage_la_recherche_continue_et_la_lecture_echoue_proprement(client_nas):
    import shutil
    avant = client_nas.faux.par_defaut
    for enfant in list(client_nas.nas.iterdir()):
        shutil.rmtree(enfant) if enfant.is_dir() and not enfant.is_symlink() else enfant.unlink()
    assert client_nas.get("/health").json()["nas"] == {"actif": True, "disponible": False}
    res = json.loads(client_nas.appli.DOCS.executer("lire_document", {"chemin": ACTE}, "t", "a"))
    assert "erreur" in res
    assert json.loads(client_nas.appli.DOCS.executer("rechercher_documents", {"texte": "achèvement"}, "t", "a"))["trouve"]


# =============================================================== désactivé par défaut / PixelKréol inchangé
def test_desactive_par_defaut_aucun_outil_aucune_trace(monkeypatch, tmp_path, faux):
    import shutil
    agents = tmp_path / "agents"
    shutil.copytree(AGENTS_ABI, agents)
    app = charger_app(monkeypatch, agents, **ENV_ABI)                  # ni PK_NAS_ACTIF ni PK_NAS_CHEMIN
    assert app.NAS_ACTIF is False
    with TestClient(app.app) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        assert app.DOCS is None and app.DOCUMENTS is None
        demande(tc)                                                     # l'archiviste ABI a bien outils: [documents]
        assert "documents" in app.REGISTRE.agents["archiviste"]["outils"]
        corps = faux.appels[0]
        assert "tools" not in corps and "tool_choice" not in corps
        assert "Outil « documents »" not in corps["messages"][0]["content"] and "LECTURE SEULE" not in corps["messages"][0]["content"]
        assert "nas" not in tc.get("/health").json()
        assert tc.get("/admin/nas", headers={"Authorization": f"Bearer {CLE_ADMIN}"}).status_code == 404
        assert tc.post("/admin/nas/reindexer", headers={"Authorization": f"Bearer {CLE_ADMIN}"}).status_code == 404


@pytest.mark.parametrize("valeur", ["0", "", "non", "false", "off", "2x"])
def test_seules_les_valeurs_explicites_activent_le_nas(monkeypatch, tmp_path, nas, valeur):
    app = charger_app(monkeypatch, AGENTS_ABI.parent / "agents", **dict(ENV_ABI, PK_NAS_ACTIF=valeur, PK_NAS_CHEMIN=str(nas)))
    assert app.NAS_ACTIF is False


def test_pixelkreol_avec_nas_actif_ses_agents_ne_changent_pas(monkeypatch, tmp_path, nas, faux):
    """Même si le NAS est activé sur une instance, les agents qui n'ont pas `outils` (tout le registre PixelKréol) ne changent pas."""
    import shutil
    agents = tmp_path / "agents"
    shutil.copytree(RACINE / "agents", agents, ignore=shutil.ignore_patterns("*.md"))
    app = charger_app(monkeypatch, agents, PK_NAS_ACTIF="1", PK_NAS_CHEMIN=str(nas), PK_NAS_INDEX=str(tmp_path / "i.sqlite"))
    with TestClient(app.app) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        assert all("outils" not in a for a in app.REGISTRE.agents.values())
        tc.post("/v1/chat/completions", headers=H, json={"model": "pk-12-secretariat", "messages": [{"role": "user", "content": "salut"}]})
        assert "tools" not in faux.appels[0] and "Outil « documents »" not in faux.appels[0]["messages"][0]["content"]


def test_pixelkreol_nas_eteint_requete_identique(appli, faux, h_api):
    """Garde-fou : une requête envoyée à LiteLLM par l'instance PixelKréol n'a ni outils ni texte NAS."""
    import asyncio  # noqa: F401
    with TestClient(appli.app) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        tc.post("/v1/chat/completions", headers=h_api, json={"model": "pk-12-secretariat", "messages": [{"role": "user", "content": "salut"}]})
        corps = faux.appels[0]
        assert set(corps) <= {"model", "messages", "temperature", "stream", "_auth"}
        assert "Outil « documents »" not in corps["messages"][0]["content"]
        assert appli.DOCS is None
        assert set(tc.get("/health").json()) == {"ok", "version", "agents", "litellm", "avertissements"}


def test_champ_outils_du_registre_valide():
    import conftest  # noqa: F401
    app = None
    from importlib import util
    spec = util.spec_from_file_location("pk_app_val", RACINE / "orchestrateur" / "app.py")
    import os
    os.environ.setdefault("LITELLM_MASTER_KEY", "x")
    app = util.module_from_spec(spec)
    spec.loader.exec_module(app)
    base = {"id": "x-agent", "num": 150, "nom": "N", "cerveau": "pk-rapide", "description": "d", "consignes": "c"}
    assert app.Registre.valider(dict(base, outils=["documents"]), set(), set()) is None
    assert app.Registre.valider(dict(base), set(), set()) is None
    for mauvais in ("documents", ["autre"], [1], {"a": 1}):
        assert "outils" in app.Registre.valider(dict(base, outils=mauvais), set(), set())
