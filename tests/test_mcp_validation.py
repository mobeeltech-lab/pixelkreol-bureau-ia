"""Client MCP (lot 3, étape 2) : validation par Will — routes d'administration, page web protégée derrière un préfixe de chemin, notification n8n,
expiration à 24 h. Faux serveur MCP et faux n8n en mémoire : aucun accès réseau."""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestrateur"))
from conftest import CLE_ADMIN, CLE_API, FauxLiteLLM  # noqa: E402
import mcp_client as M  # noqa: E402
from fabrique_mcp import FauxMCP  # noqa: E402
from test_mcp import AGENT, CONFIG, HA, MODELE, OK, demande, faux_serveurs, monter, reponse_outil  # noqa: E402

PUBLIQUE = "https://agents.mebeeltech.online/pk-validation"
P = "/pk-validation"
HOTE = {"Host": "agents.mebeeltech.online", "Origin": "https://agents.mebeeltech.online", "X-Forwarded-Proto": "https"}
BROUILLON = {"a": "client@exemple.test", "sujet": "Sujet-Secret-42", "corps": "Bonjour"}


class FauxN8N:
    def __init__(self):
        self.recus: list[dict] = []
        self.entetes: list[dict] = []
        self.statut = 200

    def __call__(self, requete: httpx.Request):
        self.recus.append(json.loads(requete.content))
        self.entetes.append(dict(requete.headers))
        return httpx.Response(self.statut, json={"ok": True})


def brancher_tout(app, c, n8n):
    routes = {"faux-pages": app._faux["pages"], "faux-courrier": app._faux["courrier"], "faux-n8n": n8n}
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: routes[r.url.host](r)))
    app.MCPC.client = client
    for s in app.MCPC.sessions.values():
        s.client = client


@pytest.fixture
def faux():
    return FauxLiteLLM()


@pytest.fixture
def n8n():
    return FauxN8N()


@pytest.fixture
def tc(monkeypatch, tmp_path, agents_dir, faux, n8n):
    monkeypatch.setenv("PK_MCP_NOTIF_JETON", "jeton-n8n-de-test")
    app = monter(monkeypatch, tmp_path, agents_dir, PK_MCP_URL_PUBLIQUE=PUBLIQUE, PK_MCP_NOTIF_URL="http://faux-n8n/webhook/pk-mcp")
    with TestClient(app.app, raise_server_exceptions=False) as c:
        c.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        brancher_tout(app, c, n8n)
        c.appli, c.faux, c.n8n = app, faux, n8n
        yield c


def mettre_en_attente(c, args=None, outil="mcp__courrier__create_draft"):
    avant = {a["id"] for a in c.appli.MCPC.base.actions()}
    c.faux.scenarios["pk-long"] = [reponse_outil([(outil, args or BROUILLON)]), OK("En attente.")]
    demande(c)
    c.faux.scenarios.clear()
    [nouvelle] = [a for a in c.appli.MCPC.base.actions() if a["id"] not in avant]
    return nouvelle


def connecter(c):
    r = c.post(f"{P}/connexion", data={"cle": CLE_ADMIN}, headers=HOTE, follow_redirects=False)
    assert r.status_code == 303
    valeur = re.search(r"pk_mcp=([^;]+)", r.headers["set-cookie"]).group(1)
    c.cookies.set("pk_mcp", valeur, path=P)             # le cookie « Secure » ne repart pas sur http://testserver : on le pose à la main
    return valeur


def jeton_de(page, verbe, ident):
    m = re.search(rf'action="{verbe}/{ident}"[^>]*><input type="hidden" name="jeton" value="([0-9a-f]+)"', page)
    assert m, (verbe, ident)
    return m.group(1)


# =============================================================== routes d'administration (clé admin)
def test_routes_admin_protegees(tc):
    a = mettre_en_attente(tc)
    for methode, chemin in (("get", "/admin/mcp/attente"), ("post", f"/admin/mcp/valider/{a['id']}"), ("post", f"/admin/mcp/refuser/{a['id']}"), ("post", f"/admin/mcp/classer/{a['id']}")):
        assert getattr(tc, methode)(chemin).status_code == 401
        assert getattr(tc, methode)(chemin, headers={"Authorization": f"Bearer {CLE_API}"}).status_code == 401       # la clé API ne suffit pas
    assert tc.appli._faux["courrier"].appels == []
    assert tc.post("/admin/mcp/valider/pas-un-id", headers=HA).status_code == 404 and tc.post("/admin/mcp/valider/0123456789", headers=HA).status_code == 404


def test_validation_execute_une_fois_avec_les_arguments_enregistres(tc):
    a = mettre_en_attente(tc)
    assert tc.appli._faux["courrier"].appels == []
    r = tc.post(f"/admin/mcp/valider/{a['id']}", headers=HA)
    assert r.status_code == 200 and r.json()["code"] == "executee"
    assert tc.appli._faux["courrier"].appels == [("create_draft", BROUILLON)]
    ligne = tc.appli.MCPC.base.action(a["id"])
    assert ligne["statut"] == "executee" and ligne["resultat"] == "brouillon créé" and ligne["decide_par"] == "admin (API)"
    assert tc.post(f"/admin/mcp/valider/{a['id']}", headers=HA).status_code == 409                       # deuxième clic : refusé
    assert len(tc.appli._faux["courrier"].appels) == 1
    assert tc.get("/admin/mcp/attente", headers=HA).json()["actions"][0]["statut"] == "executee"


def test_deux_validations_simultanees_une_seule_execution(tc):
    a = mettre_en_attente(tc)
    async def deux():
        return await asyncio.gather(tc.appli.MCPC.valider(a["id"]), tc.appli.MCPC.valider(a["id"]))
    r = tc.portal.call(deux) if hasattr(tc, "portal") and tc.portal else asyncio.run(deux())
    assert sorted(x["ok"] for x in r) == [False, True]
    assert len(tc.appli._faux["courrier"].appels) == 1


def test_refus_n_execute_rien_et_une_decision_est_definitive(tc):
    a = mettre_en_attente(tc)
    assert tc.post(f"/admin/mcp/refuser/{a['id']}", headers=HA).json()["code"] == "refusee"
    assert tc.post(f"/admin/mcp/valider/{a['id']}", headers=HA).status_code == 409
    assert tc.post(f"/admin/mcp/refuser/{a['id']}", headers=HA).status_code == 409
    assert tc.appli._faux["courrier"].appels == []


def test_action_expiree_ne_peut_plus_etre_validee(tc):
    a = mettre_en_attente(tc)
    with tc.appli.MCPC.base._con() as con:
        con.execute("UPDATE actions SET expire_le='2000-01-01T00:00:00+00:00'")
    r = tc.post(f"/admin/mcp/valider/{a['id']}", headers=HA)
    assert r.status_code == 409 and r.json()["code"] == "expiree" and tc.appli._faux["courrier"].appels == []


def test_une_action_rouge_n_est_jamais_executee_meme_validee(tc):
    a = mettre_en_attente(tc, {"a": "x@y.z", "corps": "Bonjour"}, "mcp__courrier__send_message")
    assert a["statut"] == "rouge"
    r = tc.post(f"/admin/mcp/valider/{a['id']}", headers=HA)
    assert r.status_code == 409 and tc.appli._faux["courrier"].appels == []
    assert tc.post(f"/admin/mcp/classer/{a['id']}", headers=HA).json()["code"] == "classee" and tc.appli._faux["courrier"].appels == []
    b = mettre_en_attente(tc)
    assert tc.post(f"/admin/mcp/classer/{b['id']}", headers=HA).status_code == 409                    # on ne « classe » pas une action orange en attente


def test_configuration_changee_apres_la_mise_en_attente(tc):
    a = mettre_en_attente(tc)
    o = tc.appli.MCPC.cfg.outils["mcp__courrier__create_draft"]
    o.classe = "rouge"                                                                                # l'outil est devenu un envoi
    r = tc.post(f"/admin/mcp/valider/{a['id']}", headers=HA)
    assert r.status_code == 409 and tc.appli._faux["courrier"].appels == [] and tc.appli.MCPC.base.action(a["id"])["statut"] == "refusee"
    o.classe = "orange"
    b = mettre_en_attente(tc, {"sujet": "Autre"})
    del tc.appli.MCPC.cfg.outils["mcp__courrier__create_draft"]
    assert tc.post(f"/admin/mcp/valider/{b['id']}", headers=HA).status_code == 409 and tc.appli._faux["courrier"].appels == []


def test_erreur_ou_delai_du_serveur_a_la_validation(tc):
    a = mettre_en_attente(tc)
    tc.appli._faux["courrier"].outils["create_draft"] = (tc.appli._faux["courrier"].outils["create_draft"][0],
                                                         lambda args: {"content": [{"type": "text", "text": "quota dépassé"}], "isError": True})
    r = tc.post(f"/admin/mcp/valider/{a['id']}", headers=HA)
    assert r.json()["code"] == "echec" and tc.appli.MCPC.base.action(a["id"])["statut"] == "echec"
    b = mettre_en_attente(tc, {"sujet": "B"})
    tc.appli._faux["courrier"].outils["create_draft"] = (tc.appli._faux["courrier"].outils["create_draft"][0],
                                                         lambda args: (_ for _ in ()).throw(httpx.ReadTimeout("lent")))
    r = tc.post(f"/admin/mcp/valider/{b['id']}", headers=HA)
    assert r.json()["code"] == "echec" and "vérifiez" in r.json()["message"]


# =============================================================== page web : accès, préfixe, sécurité
def test_page_exige_la_connexion_et_utilise_des_liens_relatifs(tc):
    r = tc.get(f"{P}/", headers=HOTE)
    assert r.status_code == 200 and 'action="connexion"' in r.text and 'type="password"' in r.text
    assert 'action="/' not in r.text and 'href="/' not in r.text and "http" not in re.sub(r"<style>.*?</style>", "", r.text)
    h = r.headers
    assert "default-src 'none'" in h["content-security-policy"] and h["x-frame-options"] == "DENY" and h["cache-control"] == "no-store" and "noindex" in h["x-robots-tag"]
    assert "pk_mcp" not in h.get("set-cookie", "")
    assert tc.get(f"{P}/?m=<script>alert(1)</script>", headers=HOTE).text.count("<script>") == 0         # le message n'est jamais recopié


def test_prefixe_sans_barre_finale_redirige_en_relatif(tc):
    r = tc.get(P, follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "pk-validation/"


def test_connexion_mauvaise_cle_puis_bonne_cle_cookie_limite_au_chemin(tc):
    r = tc.post(f"{P}/connexion", data={"cle": "mauvaise"}, headers=HOTE, follow_redirects=False)
    assert r.status_code == 401 and "set-cookie" not in r.headers
    r = tc.post(f"{P}/connexion", data={"cle": CLE_API}, headers=HOTE, follow_redirects=False)            # la clé API ordinaire ne suffit pas
    assert r.status_code == 401
    r = tc.post(f"{P}/connexion", data={"cle": CLE_ADMIN}, headers=HOTE, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "./?m=connecte"
    cookie = r.headers["set-cookie"]
    assert "Path=/pk-validation" in cookie and "HttpOnly" in cookie and "samesite=strict" in cookie.lower() and "Secure" in cookie and "Max-Age=28800" in cookie
    assert CLE_ADMIN not in cookie


def test_cookie_non_secure_en_local_http(tc):
    r = tc.post(f"{P}/connexion", data={"cle": CLE_ADMIN}, follow_redirects=False)
    assert r.status_code == 303 and "Secure" not in r.headers["set-cookie"] and "Path=/pk-validation" in r.headers["set-cookie"]


def test_page_liste_les_actions_avec_leurs_arguments_echappes(tc):
    a = mettre_en_attente(tc, {"sujet": "<script>alert('x')</script>", "corps": "Bonjour"})
    connecter(tc)
    page = tc.get(f"{P}/", headers=HOTE).text
    assert "En attente de votre décision (1)" in page and a["id"] in page and "create_draft" in page and "expire dans" in page
    assert "<script>alert" not in page and "&lt;script&gt;alert" in page
    assert f'action="valider/{a["id"]}"' in page and f'action="refuser/{a["id"]}"' in page and "Ce que fera la validation" in page
    assert 'action="/' not in page


def test_valider_par_la_page_avec_jeton(tc):
    a = mettre_en_attente(tc)
    cookie = connecter(tc)
    page = tc.get(f"{P}/", headers=HOTE).text
    r = tc.post(f"{P}/valider/{a['id']}", data={"jeton": jeton_de(page, "valider", a["id"])}, headers=HOTE, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "../?m=executee"
    assert tc.appli._faux["courrier"].appels == [("create_draft", BROUILLON)]
    assert tc.appli.MCPC.base.action(a["id"])["decide_par"] == "Will (page web)"
    apres = tc.get(f"{P}/", headers=HOTE).text
    assert "En attente de votre décision (0)" in apres and "executee" in apres and "Dernières décisions" in apres


def test_refuser_par_la_page(tc):
    a = mettre_en_attente(tc)
    connecter(tc)
    page = tc.get(f"{P}/", headers=HOTE).text
    r = tc.post(f"{P}/refuser/{a['id']}", data={"jeton": jeton_de(page, "refuser", a["id"])}, headers=HOTE, follow_redirects=False)
    assert r.headers["location"] == "../?m=refusee" and tc.appli._faux["courrier"].appels == []


def test_pas_d_effet_sans_session_sans_jeton_ou_avec_un_mauvais_jeton(tc):
    a = mettre_en_attente(tc)
    sans_session = tc.post(f"{P}/valider/{a['id']}", data={"jeton": "x"}, headers=HOTE, follow_redirects=False)
    assert sans_session.headers["location"] == "../?m=invalide"
    connecter(tc)
    page = tc.get(f"{P}/", headers=HOTE).text
    bon = jeton_de(page, "valider", a["id"])
    for jeton in ("", "0" * 64, jeton_de(page, "refuser", a["id"]), bon[:-1] + ("0" if bon[-1] != "0" else "1")):
        r = tc.post(f"{P}/valider/{a['id']}", data={"jeton": jeton}, headers=HOTE, follow_redirects=False)
        assert r.headers["location"] == "../?m=invalide"
    b = mettre_en_attente(tc, {"sujet": "B"})
    page = tc.get(f"{P}/", headers=HOTE).text
    r = tc.post(f"{P}/valider/{a['id']}", data={"jeton": jeton_de(page, "valider", b["id"])}, headers=HOTE, follow_redirects=False)       # jeton d'une AUTRE action
    assert r.headers["location"] == "../?m=invalide"
    assert tc.appli._faux["courrier"].appels == []


def test_origine_etrangere_refusee(tc):
    a = mettre_en_attente(tc)
    connecter(tc)
    jeton = jeton_de(tc.get(f"{P}/", headers=HOTE).text, "valider", a["id"])
    for origine in ("https://evil.example.com", "null", "http://agents.mebeeltech.online.evil.com"):
        r = tc.post(f"{P}/valider/{a['id']}", data={"jeton": jeton}, headers={**HOTE, "Origin": origine}, follow_redirects=False)
        assert r.headers["location"] == "../?m=invalide"
    assert tc.appli._faux["courrier"].appels == []


def test_cookies_falsifies_ou_perimes_refuses(tc):
    cle = CLE_ADMIN
    bon = M.nouvelle_session(cle)
    assert M.session_valide(cle, bon)
    assert not M.session_valide("autre-cle", bon) and not M.session_valide(cle, None) and not M.session_valide(cle, "") and not M.session_valide(cle, "abc")
    exp, _, sig = bon.partition(".")
    assert not M.session_valide(cle, f"{int(exp) + 999999}.{sig}")                       # date prolongée à la main
    assert not M.session_valide(cle, M.nouvelle_session(cle, time.time() - 9 * 3600))     # session de plus de 8 h
    tc.cookies.set("pk_mcp", f"{int(exp)}.{'0' * 64}", path=P)
    assert "Se connecter" in tc.get(f"{P}/", headers=HOTE).text


def test_deconnexion_efface_le_cookie(tc):
    connecter(tc)
    r = tc.post(f"{P}/deconnexion", headers=HOTE, follow_redirects=False)
    assert r.status_code == 303 and "pk_mcp=" in r.headers["set-cookie"] and ("Max-Age=0" in r.headers["set-cookie"] or "expires" in r.headers["set-cookie"].lower())


def test_actions_rouges_affichees_a_copier_et_a_classer(tc):
    a = mettre_en_attente(tc, {"a": "x@y.z", "corps": "Texte à envoyer"}, "mcp__courrier__send_message")
    connecter(tc)
    page = tc.get(f"{P}/", headers=HOTE).text
    assert "ROUGE" in page and "Texte à envoyer" in page and f'action="classer/{a["id"]}"' in page and 'action="valider/' not in page


def test_limiteur_ralentit_apres_trop_d_echecs_sans_bloquer_la_bonne_cle():
    async def essai():
        lim = M.Limiteur(seuil=3, fenetre=60, delai=0.2)
        for _ in range(3):
            assert not await lim.verifier("x", "bonne")
        t = time.monotonic()
        assert await lim.verifier("bonne", "bonne")                 # la bonne clé passe toujours, mais après le délai
        return time.monotonic() - t
    assert asyncio.run(essai()) >= 0.2


# =============================================================== préfixe configurable, absence quand MCP est coupé
def test_prefixe_personnalise_et_prefixe_invalide(monkeypatch, tmp_path, agents_dir, faux):
    app = monter(monkeypatch, tmp_path, agents_dir, PK_MCP_URL_PUBLIQUE="https://exemple.test/valid-x")
    with TestClient(app.app, raise_server_exceptions=False) as c:
        assert c.get("/valid-x/").status_code == 200 and c.get("/pk-validation/").status_code == 404
        r = c.post("/valid-x/connexion", data={"cle": CLE_ADMIN}, follow_redirects=False)
        assert "Path=/valid-x" in r.headers["set-cookie"]
    assert M.prefixe_de("https://exemple.test/Pas Valide/../x") == "/pk-validation"


def test_sans_mcp_actif_la_page_n_existe_pas(monkeypatch, tmp_path, agents_dir, faux):
    app = monter(monkeypatch, tmp_path, agents_dir, actif=False)
    with TestClient(app.app, raise_server_exceptions=False) as c:
        assert c.get(f"{P}/").status_code == 404 and c.post(f"{P}/connexion", data={"cle": CLE_ADMIN}).status_code == 404
        assert c.get("/admin/mcp/attente", headers=HA).status_code == 404


# =============================================================== notification n8n
def test_une_action_orange_declenche_un_webhook_sans_les_arguments(tc):
    a = mettre_en_attente(tc)
    assert len(tc.n8n.recus) == 1
    p = tc.n8n.recus[0]
    assert p["evenement"] == "action_en_attente" and p["id"] == a["id"] and p["outil"] == "create_draft" and p["agent"] == AGENT
    assert p["lien"] == PUBLIQUE + "/" and "Valider ou refuser : " + PUBLIQUE + "/" in p["corps"] and p["objet"].startswith("Bureau IA : action en attente")
    assert "Sujet-Secret-42" not in json.dumps(p) and "client@exemple.test" not in json.dumps(p)        # les arguments ne quittent pas le serveur
    assert tc.n8n.entetes[0]["authorization"] == "Bearer jeton-n8n-de-test"
    assert tc.appli.MCPC.base.action(a["id"])["notifie"] == "oui"


def test_pas_de_notification_pour_une_lecture_ni_pour_un_envoi_refuse(tc):
    tc.faux.scenarios["pk-long"] = [reponse_outil([("mcp__pages__fetch", {"url": "https://insee.fr/a"})]), OK("ok")]
    demande(tc)
    assert tc.n8n.recus == []


def test_echec_de_notification_ne_casse_pas_la_demande_puis_nouvel_essai(tc):
    tc.n8n.statut = 500
    tc.faux.scenarios["pk-long"] = [reponse_outil([("mcp__courrier__create_draft", BROUILLON)]), OK("En attente de validation.")]
    r = demande(tc)
    assert r.status_code == 200 and "En attente" in r.json()["choices"][0]["message"]["content"]
    a = tc.appli.MCPC.base.actions("attente")[0]
    assert a["notifie"] == "echec" and a["notif_essais"] == 1
    tc.n8n.statut = 200
    asyncio.run(tc.appli.MCPC.entretien())
    a = tc.appli.MCPC.base.action(a["id"])
    assert a["notifie"] == "oui" and a["notif_essais"] == 2 and len(tc.n8n.recus) == 2


def test_cinq_essais_au_plus(tc):
    tc.n8n.statut = 500
    a = mettre_en_attente(tc)
    for _ in range(10):
        asyncio.run(tc.appli.MCPC.entretien())
    assert tc.appli.MCPC.base.action(a["id"])["notif_essais"] == 5 and len(tc.n8n.recus) == 5


def test_entretien_fait_expirer_et_une_tache_de_fond_tourne(tc):
    a = mettre_en_attente(tc)
    with tc.appli.MCPC.base._con() as con:
        con.execute("UPDATE actions SET expire_le='2000-01-01T00:00:00+00:00'")
    assert asyncio.run(tc.appli.MCPC.entretien()) == 1 and tc.appli.MCPC.base.action(a["id"])["statut"] == "expiree"
    assert tc.app.state.taches if hasattr(tc.app.state, "taches") else tc.appli.app.state.taches


def test_url_de_notification_ou_publique_invalide_ignoree(monkeypatch, tmp_path, agents_dir, faux):
    app = monter(monkeypatch, tmp_path, agents_dir, PK_MCP_NOTIF_URL="http://hooks.exemple.com/x", PK_MCP_URL_PUBLIQUE="http://agents.exemple.com/pk-validation")
    with TestClient(app.app, raise_server_exceptions=False):
        assert app.MCPC.notif_url == "" and app.MCPC.url_publique == ""
        assert app.MCPC.lien() == "http://127.0.0.1:4001/pk-validation/"                              # repli : accès local


def test_etat_indique_notification_et_page(tc):
    e = tc.get("/admin/mcp/etat", headers=HA).json()
    assert e["notification"] is True and e["page_de_validation"] == PUBLIQUE + "/"
    assert "jeton-n8n-de-test" not in json.dumps(e)


# =============================================================== fichiers de l'étape 2
def test_modele_n8n_et_compose_sans_secret():
    racine = Path(__file__).resolve().parent.parent
    wf = json.loads((racine / "scripts" / "mcp" / "n8n-notification-mcp.json").read_text(encoding="utf-8"))
    assert [n["type"] for n in wf["nodes"]] == ["n8n-nodes-base.webhook", "n8n-nodes-base.emailSend"] and wf["active"] is False
    assert "A-CONFIGURER" in json.dumps(wf) and "@gmail" not in json.dumps(wf)                  # aucune adresse ni identifiant réel dans le dépôt
    compose = (racine / "docker-compose.mcp.yml").read_text(encoding="utf-8")
    assert "PK_MCP_URL_PUBLIQUE" in compose and "PK_MCP_NOTIF_URL" in compose and "PK_MCP_NOTIF_JETON: ${PK_MCP_NOTIF_JETON:-}" in compose


def test_exemple_contient_les_ecritures_seulement_en_commentaire():
    racine = Path(__file__).resolve().parent.parent
    texte = (racine / "agents" / "mcp.exemple.yaml").read_text(encoding="utf-8")
    assert "classe: orange" in texte and "classe: rouge" in texte
    assert all(o.classe == "vert" for o in M.charger_config(racine / "agents" / "mcp.exemple.yaml").outils.values())
