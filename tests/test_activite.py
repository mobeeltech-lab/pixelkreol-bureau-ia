"""Journal d'activité : qui demande quoi, quel maillon répond. Le journal ne doit jamais ralentir ni bloquer une réponse,
ne jamais garder une clé ou une donnée personnelle, et rester sans texte de demande pour ABI Holding."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from conftest import CLE_API, CLE_ADMIN, FauxLiteLLM, charger_app

OK = FauxLiteLLM.ok


def msg(texte="salut"):
    return [{"role": "user", "content": texte}]


class PoolEnregistreur:
    """Faux pool Postgres : note les écritures au lieu de les envoyer."""

    def __init__(self):
        self.ecritures: list[tuple[str, tuple]] = []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, *args):
        self.ecritures.append((sql, args))

    async def close(self):
        pass

    def journal(self):
        return [a for s, a in self.ecritures if "INTO pk_journal" in s]

    def maillons(self):
        return [a for s, a in self.ecritures if "INTO pk_maillons" in s]


def attendre(pool, quoi="journal", n=1, delai=3.0):
    """Les écritures partent en arrière-plan : on attend qu'elles soient arrivées (jamais plus de `delai` secondes)."""
    fin = time.time() + delai
    while time.time() < fin:
        if len(getattr(pool, quoi)()) >= n:
            return
        time.sleep(0.02)


def demander(client, h_api, texte, entetes=None, modele="pk-12-secretariat"):
    return client.post("/v1/chat/completions", headers={**h_api, **(entetes or {})},
                       json={"model": modele, "messages": msg(texte)})


# ---------------------------------------------------------------------------------------------
#  Sujet : rien de secret ni de personnel
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("texte,interdit", [
    ("utilise la clé sk-" + "abcdefghijklmnopqrstuvwxyz123456 pour l'API", "sk-abcdefghij"),
    ("ma clé gsk_" + "ABCDEFGHIJKLMNOP1234567890 ne marche plus", "gsk_ABCDEFGH"),
    ("NVIDIA nvapi-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789 à tester", "nvapi-AbCdEf"),
    ("mon mot de passe est Hunter2Secret!", "Hunter2Secret"),
    ("password: S3cr3tValue merci", "S3cr3tValue"),
    ("écris à jean.dupont@exemple.fr pour le devis", "jean.dupont"),
    ("rappelle-moi au 06 12 34 56 78 demain", "12 34 56"),
    ("rappelle au +262 692 12 34 56", "692 12 34"),
    ("vire sur FR76 3000 6000 0112 3456 7890 189 svp", "3000 6000"),
    ("carte 4970 1012 3456 7890 expire en 12/28", "4970 1012"),
])
def test_le_sujet_ne_garde_ni_secret_ni_donnee_personnelle(appli, texte, interdit):
    sujet = appli.masquer_sujet(texte)
    assert interdit not in sujet, sujet
    assert len(sujet) <= 80


def test_une_cle_coupee_en_deux_par_la_limite_de_80_caracteres_ne_laisse_pas_son_debut(appli):
    texte = "x" * 70 + " sk-" + "abcdefghijklmnopqrstuvwxyz123456"
    assert "sk-" not in appli.masquer_sujet(texte)


def test_le_sujet_garde_le_debut_de_la_demande(appli):
    assert appli.masquer_sujet("Rédige un post Instagram pour la boulangerie de Saint-Denis") .startswith("Rédige un post Instagram")
    assert len(appli.masquer_sujet("a " * 200)) == 80


# ---------------------------------------------------------------------------------------------
#  Ce que le journal enregistre
# ---------------------------------------------------------------------------------------------
def test_le_journal_enregistre_demandeur_sujet_et_fournisseur(client, h_api, faux):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    faux.par_defaut = lambda c: httpx.Response(200, json={**OK().json(), "model": "mistral-small-latest"})
    r = demander(client, h_api, "Rédige un post Instagram pour la boulangerie",
                 {"x-openwebui-user-name": "Marie", "x-openwebui-user-id": "u42"})
    assert r.status_code == 200
    attendre(pool)
    agent, mode, cerveau, duree, ok, detail, demandeur, sujet, fournisseur = pool.journal()[0]
    assert (agent, ok, demandeur, fournisseur) == ("secretariat", True, "Marie (u42)", "mistral-small-latest")
    assert sujet == "Rédige un post Instagram pour la boulangerie"
    assert cerveau.startswith("pk-")


def test_le_fournisseur_est_aussi_lu_dans_un_flux(client, h_api, faux):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    bloc = b'data: {"id":"c","object":"chat.completion.chunk","model":"gemini-flash-lite-latest","choices":[{"index":0,"delta":{"content":"a"}}]}\n\n'
    faux.par_defaut = lambda c: FauxLiteLLM.flux([bloc, b"data: [DONE]\n\n"])
    r = client.post("/v1/chat/completions", headers=h_api, json={"model": "pk-12-secretariat", "stream": True, "messages": msg()})
    assert r.status_code == 200
    attendre(pool)
    assert pool.journal()[0][-1] == "gemini-flash-lite-latest"


def test_la_demande_sans_nom_est_attribuee_a_api(client, h_api):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    demander(client, h_api, "salut")
    attendre(pool)
    assert pool.journal()[0][6] == "api"


def test_les_essais_de_chaque_cerveau_sont_notes_avec_leur_resultat(client, h_api, faux):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    premier = client.appli.REGISTRE.agents["secretariat"]
    cerveaux = list(client.appli.preparer_corps({"messages": msg()}, premier)[1])
    assert len(cerveaux) >= 2
    faux.scenarios[cerveaux[0]] = [FauxLiteLLM.statut(429)]
    faux.par_defaut = OK()
    assert demander(client, h_api, "salut").status_code == 200
    attendre(pool, "maillons", 2)
    resultats = [(a[0], a[2], a[3]) for a in pool.maillons()]
    assert (cerveaux[0], 429, "429") in resultats
    assert (cerveaux[1], 200, "ok") in resultats


def test_aucune_cle_ne_ressort_dans_le_sujet_ni_le_detail(client, h_api, faux):
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    secrets = ["sk-" + "abcdefghijklmnopqrstuvwxyz123456", "gsk_" + "ABCDEFGHIJKLMNOP1234567890",
               "nvapi-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "Hunter2Secret"]
    faux.par_defaut = FauxLiteLLM.statut(401, "clé sk-" + "abcdefghijklmnopqrstuvwxyz123456 refusée")
    demander(client, h_api, f"clés {secrets[0]} {secrets[1]} {secrets[2]} mot de passe: {secrets[3]}")
    attendre(pool)
    tout = repr(pool.ecritures)
    for s in secrets:
        assert s not in tout


def test_la_demande_en_cours_apparait_puis_disparait(client, h_api, faux):
    vues = []

    def espion(corps):
        vues.append(client.appli.JOURNAL.demandes_en_cours())
        return OK()
    faux.par_defaut = espion
    demander(client, h_api, "Prépare le devis", {"x-openwebui-user-name": "Will"})
    assert vues[0] and vues[0][0]["demandeur"].startswith("Will") and vues[0][0]["agent"] == "secretariat"
    assert client.appli.JOURNAL.demandes_en_cours() == []


# ---------------------------------------------------------------------------------------------
#  ABI Holding : aucun texte de demande, même par erreur de réglage
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def client_abi(monkeypatch, agents_dir, faux):
    from fastapi.testclient import TestClient
    appli = charger_app(monkeypatch, agents_dir, PK_MARQUE="ABI Holding", PK_JOURNAL_SUJET="1")
    with TestClient(appli.app, raise_server_exceptions=False) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        tc.appli = appli
        yield tc


def test_abi_ne_garde_aucun_sujet_meme_si_le_reglage_est_mis_par_erreur(client_abi, h_api, faux):
    assert client_abi.appli.SUJET_ACTIF is False
    pool = PoolEnregistreur()
    client_abi.appli.JOURNAL.pool = pool
    demander(client_abi, h_api, "Contrat confidentiel du client Dupont, montant 120 000 euros", {"x-openwebui-user-name": "Paul"})
    attendre(pool)
    ligne = pool.journal()[0]
    assert ligne[7] == ""                                   # sujet
    assert "Dupont" not in repr(pool.ecritures)
    assert all(e["sujet"] == "" for e in client_abi.get("/admin/activite", headers={"Authorization": f"Bearer {CLE_ADMIN}"}).json()["en_cours"])


def test_abi_sujet_vide_dans_les_demandes_en_cours(client_abi, h_api, faux):
    vues = []
    faux.par_defaut = lambda c: (vues.append(client_abi.appli.JOURNAL.demandes_en_cours()), OK())[1]
    demander(client_abi, h_api, "Texte secret d'ABI")
    assert vues[0][0]["sujet"] == ""


def test_pixelkreol_peut_couper_le_sujet_avec_le_reglage(monkeypatch, agents_dir):
    appli = charger_app(monkeypatch, agents_dir, PK_JOURNAL_SUJET="0")
    assert appli.SUJET_ACTIF is False
    monkeypatch.delenv("PK_JOURNAL_SUJET")
    assert charger_app(monkeypatch, agents_dir).SUJET_ACTIF is True    # PixelKréol : allumé par défaut


# ---------------------------------------------------------------------------------------------
#  Route admin
# ---------------------------------------------------------------------------------------------
def test_route_activite_refusee_sans_cle(client):
    assert client.get("/admin/activite").status_code == 401
    assert client.get("/admin/activite", headers={"Authorization": "Bearer mauvaise"}).status_code == 401
    assert client.post("/admin/activite/tester").status_code == 401


def test_route_activite_refusee_avec_la_cle_api_simple(client, h_api):
    assert client.get("/admin/activite", headers=h_api).status_code == 401      # la clé admin est distincte
    assert client.post("/admin/activite/tester", headers=h_api).status_code == 401


def test_route_activite_repond_avec_la_cle_admin_meme_sans_postgres(client, h_admin):
    r = client.get("/admin/activite", headers=h_admin)
    assert r.status_code == 200
    j = r.json()
    assert j["journal_postgres"] is False and j["dernieres"] == [] and j["sujet_actif"] is True


def test_le_bouton_tester_teste_chaque_cerveau_sans_bascule(client, h_admin, faux):
    ids = ["pk-rapide", "pk-rapide--2", "pk-code"]

    def serveur(requete):
        if requete.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        return None
    ancien = faux.__call__

    def appel(requete):
        if requete.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        return ancien(requete)
    client.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(appel))
    faux.scenarios["pk-rapide--2"] = [FauxLiteLLM.statut(402, "solde épuisé")]
    faux.par_defaut = OK()
    pool = PoolEnregistreur()
    client.appli.JOURNAL.pool = pool
    j = client.post("/admin/activite/tester", headers=h_admin).json()
    assert j["total"] == 3 and j["bons"] == 2 and j["ok"] is False
    assert {x["maillon"]: x["resultat"] for x in j["resultats"]}["pk-rapide--2"] == "402"
    assert all(a["disable_fallbacks"] is True for a in faux.appels)
    attendre(pool, "maillons", 3)
    assert {a[4] for a in pool.maillons()} == {"test"}


# ---------------------------------------------------------------------------------------------
#  Le journal ne ralentit rien et une panne ne bloque rien
# ---------------------------------------------------------------------------------------------
def test_ecrire_rend_la_main_aussitot_meme_si_postgres_est_bloque(appli):
    class PoolBloque(PoolEnregistreur):
        async def execute(self, sql, *args):
            await asyncio.sleep(60)

    async def mesure():
        appli.JOURNAL.pool = PoolBloque()
        t = time.monotonic()
        for _ in range(20):
            await appli.JOURNAL.ecrire("secretariat", "direct", "pk-rapide", time.monotonic(), True)
            appli.JOURNAL.maillon("pk-rapide", 200, "x", "trafic", 5)
        return time.monotonic() - t
    assert asyncio.run(mesure()) < 0.1


def test_la_reponse_n_attend_pas_un_postgres_qui_ne_repond_plus(client, h_api):
    class PoolBloque(PoolEnregistreur):
        async def execute(self, sql, *args):
            await asyncio.sleep(60)
    client.appli.JOURNAL.pool = PoolBloque()
    debut = time.monotonic()
    for _ in range(3):
        assert demander(client, h_api, "salut").status_code == 200
    assert time.monotonic() - debut < 2.0


def test_trop_d_ecritures_en_attente_sont_abandonnees_sans_erreur(appli):
    class PoolBloque(PoolEnregistreur):
        async def execute(self, sql, *args):
            await asyncio.sleep(60)

    async def inonder():
        appli.JOURNAL.pool = PoolBloque()
        for _ in range(appli.ECRITURES_MAX + 50):
            await appli.JOURNAL.ecrire("a", "direct", "pk-rapide", time.monotonic(), True)
        return appli.JOURNAL.abandons, len(appli.JOURNAL.attente)
    abandons, attente = asyncio.run(inonder())
    assert abandons == 50 and attente == appli.ECRITURES_MAX


def test_le_cout_du_journal_est_de_quelques_millisecondes(appli):
    async def mesure():
        appli.JOURNAL.pool = PoolEnregistreur()
        t = time.monotonic()
        for _ in range(100):
            appli.CTX_DEMANDE.set({"id": "", "demandeur": "Will", "sujet": appli.masquer_sujet("Rédige un post"), "fournisseur": "m"})
            await appli.JOURNAL.ecrire("secretariat", "direct", "pk-rapide", time.monotonic(), True)
        return (time.monotonic() - t) / 100
    assert asyncio.run(mesure()) < 0.005       # moins de 5 ms par demande (masquage du sujet compris)
