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
ENV_ABI = {"PK_MARQUE": "ABI Holding", "PK_LIEU": "à Ajaccio", "PK_FUSEAU": "Europe/Paris", "PK_PATRON": "la direction d'ABI Holding",
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
    assert etat["actif"] and etat["fichiers_indexes"] == 11 and etat["fts5"]
    (client_nas.nas / "Nouveau.txt").write_text("DONNÉES FICTIVES — TEST nouveau", encoding="utf-8")
    assert client_nas.post("/admin/nas/reindexer").status_code == 401
    assert client_nas.post("/admin/nas/reindexer", headers=h).status_code == 202
    for _ in range(50):
        if client_nas.get("/admin/nas", headers=h).json()["fichiers_indexes"] == 12:
            break
        time.sleep(0.1)
    assert client_nas.get("/admin/nas", headers=h).json()["fichiers_indexes"] == 12


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


# =============================================================== appels d'outils écrits en TEXTE (modèles type Qwen)
BALISES = ("<tool_call", "</tool_call", "<function=", "</function", "<parameter=", "</parameter")
COMPROMIS = "Oliviers/juridique/compromis-vente-A05.txt"
APPEL_QWEN = ("<tool_call> <function=rechercher_documents> <parameter=texte> compromis </parameter> "
              "<parameter=type> pdf,docx </parameter> </function> </tool_call>")           # le texte exact observé sur le serveur


def texte_stream(r):
    return "".join(json.loads(l[6:])["choices"][0]["delta"].get("content") or "" for l in r.text.splitlines() if l.startswith("data: {"))


def sans_balise(texte):
    return not any(b in texte for b in BALISES)


def test_cas_reel_appel_qwen_en_texte_execute_puis_reponse_propre(client_nas):
    f = client_nas.faux
    f.scenarios["pk-long"] = [OK(APPEL_QWEN), OK(f"Le compromis est ici : {COMPROMIS}")]
    r = demande(client_nas, "Retrouve le compromis de vente")
    contenu = r.json()["choices"][0]["message"]["content"]
    assert r.status_code == 200 and sans_balise(contenu) and COMPROMIS in contenu and "Non vérifié" not in contenu
    assert len(f.appels) == 2
    second = f.appels[1]["messages"]
    appel = [m for m in second if m["role"] == "assistant" and m.get("tool_calls")][0]["tool_calls"][0]
    assert appel["function"]["name"] == "rechercher_documents" and json.loads(appel["function"]["arguments"])["type"] == "pdf,docx"
    outil = [m for m in second if m["role"] == "tool"][0]
    assert outil["tool_call_id"] == appel["id"]
    resultat = json.loads(outil["content"])
    assert resultat["trouve"] and resultat["resultats"][0]["chemin"] == COMPROMIS            # type « pdf,docx » ignoré : le .txt est trouvé
    assert r.json()["usage"]["total_tokens"] == 24
    assert any(l["action"] == "recherche" for l in journal(client_nas))


def test_appel_json_entre_balises_execute(client_nas):
    f = client_nas.faux
    f.scenarios["pk-long"] = [OK('<tool_call>{"name": "lire_document", "arguments": {"chemin": "%s"}}</tool_call>' % COMPROMIS), OK("Prix : 171 500,00 euros")]
    r = demande(client_nas, "Quel est le prix du compromis ?")
    assert sans_balise(r.json()["choices"][0]["message"]["content"]) and "171 500,00" in r.json()["choices"][0]["message"]["content"]
    tool = [m for m in f.appels[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "171 500,00" in tool and COMPROMIS in tool
    assert [l["action"] for l in journal(client_nas)] == ["lecture"]


def test_appel_en_texte_avec_phrase_avant_l_appel(client_nas):
    f = client_nas.faux
    f.scenarios["pk-long"] = [OK("Je vais chercher dans le NAS.\n" + APPEL_QWEN), OK(f"Trouvé : {COMPROMIS}")]
    contenu = demande(client_nas).json()["choices"][0]["message"]["content"]
    assert sans_balise(contenu) and COMPROMIS in contenu and len(f.appels) == 2


def test_appels_en_texte_en_serie_puis_reponse(client_nas):
    f = client_nas.faux
    lecture = '<tool_call><function=lire_document><parameter=chemin>%s</parameter></function></tool_call>' % COMPROMIS
    f.scenarios["pk-long"] = [OK(APPEL_QWEN), OK(lecture), OK("Voilà, tout est dit.")]
    contenu = demande(client_nas).json()["choices"][0]["message"]["content"]
    assert contenu == "Voilà, tout est dit." and len(f.appels) == 3
    assert [l["action"] for l in journal(client_nas)] == ["recherche", "lecture"]


def test_boucle_d_appels_en_texte_bornee_et_jamais_de_balise_a_l_utilisateur(client_nas):
    f = client_nas.faux
    f.par_defaut = OK(APPEL_QWEN)                       # le modèle ne fait que réclamer des outils, en texte
    r = demande(client_nas)
    contenu = r.json()["choices"][0]["message"]["content"]
    assert r.status_code == 200 and sans_balise(contenu) and contenu.strip()
    assert len(f.appels) <= 4 + 1 + 1                   # 4 tours, un dernier appel pour conclure, au plus un repli


def test_appel_en_texte_illisible_utilise_le_repli_sur_l_index(client_nas):
    f = client_nas.faux

    def repond(corps):
        if "tools" in corps:
            return OK('<tool_call>{"name": "rechercher_documents", "arguments": {broken </tool_call>')
        return OK(f"D'après l'index : {COMPROMIS}")
    f.par_defaut = repond
    contenu = demande(client_nas, "Retrouve le compromis de vente du lot A05").json()["choices"][0]["message"]["content"]
    assert sans_balise(contenu) and COMPROMIS in contenu
    assert len(f.appels) == 2 and "tools" in f.appels[0] and "tools" not in f.appels[1]
    assert "Documents trouvés dans le NAS" in f.appels[1]["messages"][0]["content"] and COMPROMIS in f.appels[1]["messages"][0]["content"]


def test_repli_dont_la_reponse_contient_encore_des_balises_est_nettoyee(client_nas):
    f = client_nas.faux
    f.par_defaut = OK("<tool_call>{cassé}</tool_call>")
    r = demande(client_nas, "Retrouve le compromis")
    contenu = r.json()["choices"][0]["message"]["content"]
    assert r.status_code == 200 and sans_balise(contenu)
    assert "NAS" in contenu and "pas pu" in contenu                  # message clair plutôt qu'un texte vide ou des balises


def test_balises_restantes_apres_du_texte_utile_sont_retirees(client_nas):
    f = client_nas.faux
    f.par_defaut = OK("Le prix est de 171 500,00 euros. <tool_call> <function=lire_document> </function> </tool_call>")
    contenu = demande(client_nas).json()["choices"][0]["message"]["content"]
    assert sans_balise(contenu) and "171 500,00" in contenu


def test_jamais_de_balise_en_flux(client_nas):
    f = client_nas.faux
    f.scenarios["pk-long"] = [OK(APPEL_QWEN), OK(f"Trouvé : {COMPROMIS}")]
    r = demande(client_nas, stream=True)
    texte = texte_stream(r)
    assert sans_balise(texte) and COMPROMIS in texte and r.text.rstrip().endswith("data: [DONE]")
    f.appels.clear()
    f.par_defaut = OK("<tool_call>{cassé}</tool_call>")
    assert sans_balise(texte_stream(demande(client_nas, stream=True)))


def test_jamais_de_balise_en_mode_repli_force(monkeypatch, tmp_path, nas, faux):
    app = monter(monkeypatch, tmp_path, nas, faux, PK_NAS_MODE="repli")
    with TestClient(app.app) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        app.DOCS.indexer()
        faux.par_defaut = OK("<tool_call><function=rechercher_documents><parameter=texte>x</parameter></function></tool_call>")
        contenu = demande(tc, "compromis").json()["choices"][0]["message"]["content"]
        assert sans_balise(contenu) and contenu.strip()


def test_pk_equipe_aussi(client_nas):
    f = client_nas.faux

    def repond(corps):
        user = corps["messages"][-1]["content"]
        if "PLANIFICATION_JSON" in user:
            return OK(json.dumps({"taches": [{"agent": "archiviste", "consigne": "retrouve le compromis"}]}))
        if "contributions de l'équipe" in user:
            return OK("Synthèse")
        if any(m["role"] == "tool" for m in corps["messages"]):
            return OK(f"Trouvé {COMPROMIS}")
        return OK(APPEL_QWEN)
    f.par_defaut = repond
    r = client_nas.post("/v1/chat/completions", headers=H, json={"model": "pk-equipe", "messages": [{"role": "user", "content": "Retrouve le compromis"}]})
    assert r.status_code == 200 and sans_balise(r.json()["choices"][0]["message"]["content"]) and COMPROMIS in r.json()["choices"][0]["message"]["content"]


def test_les_vrais_tool_calls_continuent_de_marcher(client_nas):
    f = client_nas.faux
    f.scenarios["pk-long"] = [reponse_outil("rechercher_documents", {"texte": "compromis", "type": "pdf,docx"}), OK(f"Trouvé : {COMPROMIS}")]
    contenu = demande(client_nas).json()["choices"][0]["message"]["content"]
    assert COMPROMIS in contenu and len(f.appels) == 2
    resultat = json.loads([m for m in f.appels[1]["messages"] if m["role"] == "tool"][0]["content"])
    assert resultat["resultats"][0]["chemin"] == COMPROMIS


def test_nas_eteint_aucun_traitement_des_balises(monkeypatch, tmp_path, faux):
    """Instance sans NAS : la réponse du modèle est transmise telle quelle (comportement PixelKréol inchangé)."""
    import shutil
    agents = tmp_path / "agents"
    shutil.copytree(AGENTS_ABI, agents)
    app = charger_app(monkeypatch, agents, **ENV_ABI)
    with TestClient(app.app) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        faux.par_defaut = OK("texte avec <tool_call> inattendu </tool_call>")
        assert demande(tc).json()["choices"][0]["message"]["content"] == "texte avec <tool_call> inattendu </tool_call>"
        assert len(faux.appels) == 1
