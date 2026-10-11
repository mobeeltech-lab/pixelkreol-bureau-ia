"""Instance de TEST « ABI Holding » (instances/abi) : registre, routage, garde-fous, compose, nginx, données fictives."""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
import unicodedata
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cas_routage_abi import CAS, CAS_CONTROLE  # noqa: E402
from conftest import CLE_API, RACINE, FauxLiteLLM, charger_app  # noqa: E402

ABI = RACINE / "instances" / "abi"
AGENTS_ABI = ABI / "agents"
IDS = ["orchestrateur", "secretaire", "comptable", "appels-de-fonds", "commercial", "locatif", "juriste",
       "suivi-programme", "archiviste"]
ENV_ABI = {"PK_MARQUE": "ABI Holding", "PK_LIEU": "en Corse", "PK_FUSEAU": "Europe/Paris",
           "PK_PATRON": "la direction d'ABI Holding", "PK_VITRINE_PIED": "environnement de test · données fictives",
           "PK_PROPRIETAIRE": "abi"}


def sans_accents(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", t.lower()) if not unicodedata.combining(c))


def charge(nom):
    return yaml.safe_load((AGENTS_ABI / nom).read_text(encoding="utf-8"))


@pytest.fixture
def abi_dir(tmp_path):
    dest = tmp_path / "agents"
    shutil.copytree(AGENTS_ABI, dest)
    return dest


@pytest.fixture
def app_abi(monkeypatch, abi_dir):
    return charger_app(monkeypatch, abi_dir, **ENV_ABI)


@pytest.fixture
def client_abi(app_abi, faux):
    with TestClient(app_abi.app, raise_server_exceptions=False) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        tc.faux, tc.appli = faux, app_abi
        yield tc


# =============================================================== 1. registre
def test_neuf_agents_dans_le_bon_ordre():
    agents = charge("registre.yaml")["agents"]
    assert [a["id"] for a in agents] == IDS
    assert [a["num"] for a in agents] == list(range(1, 10))


def test_ids_et_numeros_uniques_et_champs_complets():
    agents = charge("registre.yaml")["agents"]
    assert len({a["id"] for a in agents}) == 9 and len({a["num"] for a in agents}) == 9
    for a in agents:
        for champ in ("id", "num", "nom", "emoji", "famille", "cerveau", "temperature", "description", "mots_cles", "consignes"):
            assert a.get(champ) not in (None, "", []), f"{a['id']} : {champ} manquant"
        assert len(a["mots_cles"]) >= 8, f"{a['id']} : mots-clés trop pauvres"
        assert len(a["consignes"]) > 400, f"{a['id']} : consignes trop courtes"


def test_cerveaux_utilises_existent_dans_cerveaux_yaml():
    cerveaux = charge("cerveaux.yaml")
    connus = set(cerveaux["cerveaux"])
    assert connus == {"pk-rapide", "pk-costaud", "pk-code", "pk-vision", "pk-long"}
    for a in charge("registre.yaml")["agents"]:
        assert a["cerveau"] in connus, f"{a['id']} : cerveau {a['cerveau']} inconnu"
    for c, suite in cerveaux["secours_entre_cerveaux"].items():
        assert c in connus and set(suite) <= connus


def test_le_registre_abi_est_accepte_sans_aucun_avertissement(app_abi):
    assert list(app_abi.REGISTRE.agents) == IDS
    assert app_abi.REGISTRE.avertissements == []
    assert app_abi.REGISTRE.socle.startswith("Tu fais partie du Bureau IA d'ABI Holding")


def test_le_registre_abi_suit_le_meme_format_que_pixelkreol():
    ref = yaml.safe_load((RACINE / "agents" / "registre.yaml").read_text(encoding="utf-8"))["agents"]
    cles_ref = set(ref[0])
    for a in charge("registre.yaml")["agents"]:
        assert set(a) == cles_ref, f"{a['id']} : champs différents du registre PixelKréol"


def test_perso_abi_vide_et_valide():
    assert charge("perso.yaml") == {"agents": []}


def test_les_neuf_agents_repondent_via_l_orchestrateur(client_abi, h_api):
    modeles = [m["id"] for m in client_abi.get("/v1/models", headers=h_api).json()["data"]]
    assert modeles[:2] == ["pk-auto", "pk-equipe"] and len(modeles) == 11
    for ident, num in zip(IDS, range(1, 10)):
        r = client_abi.post("/v1/chat/completions", headers=h_api, json={
            "model": f"pk-{num:02d}-{ident}", "messages": [{"role": "user", "content": "bonjour"}]})
        assert r.status_code == 200 and r.headers["x-pk-agent"] == ident


# =============================================================== 2. routage
def router(app, faux, texte):
    client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
    return asyncio.run(app.choisir_agent(client, [{"role": "user", "content": texte}]))


@pytest.fixture
def ia_panne(faux):
    faux.par_defaut = FauxLiteLLM.statut(429, "quota")
    return faux


def test_au_moins_40_demandes_de_routage():
    assert len(CAS) >= 40 and len({t for t, _ in CAS}) == len(CAS)
    assert {a for _, a in CAS} == set(IDS)                    # chaque agent est visé
    assert all(sum(1 for _, a in CAS if a == i) >= 4 for i in IDS)


def test_taux_de_bonne_orientation_sans_ia(app_abi, ia_panne):
    faux = [f"{att} <- {router(app_abi, ia_panne, t)[0]['id']} : {t[:60]}" for t, att in CAS
            if router(app_abi, ia_panne, t)[0]["id"] != att]
    taux = 1 - len(faux) / len(CAS)
    assert taux >= 0.92, f"{taux:.0%} ; erreurs : {faux}"


def test_jeu_de_controle_non_regle_reste_acceptable(app_abi, ia_panne):
    faux = [t for t, att in CAS_CONTROLE if router(app_abi, ia_panne, t)[0]["id"] != att]
    assert 1 - len(faux) / len(CAS_CONTROLE) >= 0.75, faux


@pytest.mark.parametrize("texte,attendu", [
    ("prépare l'appel de fonds hors d'eau pour les Oliviers", "appels-de-fonds"),
    ("retrouve le PV de livraison du lot B12", "archiviste"),
    ("révise le loyer de M. Fictif selon l'IRL", "locatif"),
])
def test_exemples_du_cahier_des_charges(app_abi, ia_panne, texte, attendu):
    agent, methode = router(app_abi, ia_panne, texte)
    assert agent["id"] == attendu and methode == "mots-clés" and not ia_panne.appels   # tranché sans appel à l'IA


@pytest.mark.parametrize("texte", ["APPEL DE FONDS HORS D'EAU", "Appel de fonds hors d eau", "apel de fond mise hors deau",
                                   "Quitance du locatère", "QUITTANCE DE LOYER"])
def test_majuscules_accents_et_fautes_frequentes(app_abi, ia_panne, texte):
    agent, _ = router(app_abi, ia_panne, texte)
    assert agent["id"] in ("appels-de-fonds", "locatif")


def test_aucun_mot_cle_partage_entre_deux_agents_abi(app_abi):
    vus, doublons = {}, []
    for a in app_abi.REGISTRE.agents.values():
        for m in a["_mots"]:
            if m in vus and vus[m] != a["id"]:
                doublons.append(f"« {m} » : {vus[m]} et {a['id']}")
            vus[m] = a["id"]
    assert not doublons, doublons


def test_chaque_agent_abi_est_atteignable_par_ses_mots_cles(app_abi, ia_panne):
    rates = []
    for a in app_abi.REGISTRE.agents.values():
        if a["id"] == "orchestrateur":
            continue
        texte = " ".join(a["mots_cles"][:4])
        if router(app_abi, ia_panne, texte)[0]["id"] != a["id"]:
            rates.append(a["id"])
    assert not rates, rates


def test_sans_ia_et_sans_indice_le_directeur_prend_la_main(app_abi, ia_panne):
    agent, methode = router(app_abi, ia_panne, "hmm ok")
    assert agent["id"] == "orchestrateur" and methode == "défaut"


# =============================================================== 3. garde-fous
def textes_abi():
    return {nom: (AGENTS_ABI / nom).read_text(encoding="utf-8") for nom in ("registre.yaml", "socle.txt", "cerveaux.yaml", "perso.yaml")}


INTERDITS = ["pixelkreol", "kap numerik", "bureau ia local", "assistant maison", "bureau essentiel", "bureau complet",
             "presence en ligne", "mebeeltech", "fabrique d'agents", "community manager", "redacteur", "prospecteur 974",
             "will", "la reunion", "974", "tarif pixel"]


@pytest.mark.parametrize("nom", ["registre.yaml", "socle.txt", "cerveaux.yaml", "perso.yaml"])
def test_aucune_mention_de_pixelkreol_dans_le_registre_et_le_socle_abi(nom):
    texte = sans_accents(textes_abi()[nom])
    trouves = [m for m in INTERDITS if re.search(rf"(?<![a-z0-9]){re.escape(sans_accents(m))}(?![a-z0-9])", texte)]
    assert not trouves, f"{nom} mentionne : {trouves}"


def test_pas_de_prix_ni_d_offre_pixelkreol_dans_le_registre_abi():
    texte = textes_abi()["registre.yaml"] + textes_abi()["socle.txt"]
    assert not re.search(r"\b\d[\d  ]*\s?€\s?(HT|TTC)", texte), "montant en euros inattendu dans les consignes"


def test_le_socle_dit_test_donnees_fictives_et_les_trois_feux():
    socle = sans_accents(textes_abi()["socle.txt"])
    assert "environnement de test, donnees fictives" in socle
    for mot in ("vert", "orange", "rouge", "24 h", "oui"):
        assert mot in socle
    # actions ROUGE listées
    for rouge in ("appel de fonds", "mise en demeure", "bail", "contrat", "ecriture comptable validee", "paiement",
                  "avocat", "banque", "notaire", "administration"):
        assert rouge in socle, rouge
    # actions VERT / ORANGE listées
    for mot in ("classer", "resumer", "preparer", "calculer", "rappeler", "quittances", "relance amiable",
                "reponse a un fournisseur", "courrier standard"):
        assert mot in socle, mot
    assert "n'inventes jamais" in socle


def consignes(ident):
    brut = next(a for a in charge("registre.yaml")["agents"] if a["id"] == ident)["consignes"]
    return re.sub(r"\s+", " ", sans_accents(brut))


@pytest.mark.parametrize("ident,attendus", [
    ("appels-de-fonds", ["rouge", "jamais d'envoi sans", "oui", "r261-14", "35 %", "70 %", "95 %", "echeancier contractuel",
                         "mise en demeure", "lettre par acquereur", "attestation d'avancement"]),
    ("comptable", ["prepare", "ne valides jamais", "rouge", "paiement", "expert-comptable", "rapprochement bancaire"]),
    ("locatif", ["quittance", "vert", "a jour", "irl", "relance amiable", "orange", "mise en demeure", "rouge", "bail"]),
    ("juriste", ["a faire valider par le conseil", "rouge", "bail", "avocat", "notaire", "banque", "administration"]),
    ("secretaire", ["j-7", "j-1", "orange", "rouge", "avocat", "banque", "notaire", "administration"]),
    ("commercial", ["grille", "valid", "rouge"]),
    ("suivi-programme", ["planning", "situation", "reserve", "tableau de bord", "appels de fonds", "rouge"]),
    ("archiviste", ["ne supprimes jamais", "doublon", "pieces manquantes", "rouge"]),
    ("orchestrateur", ["a l'identique", "ecart", "rouge", "j-7"]),
])
def test_les_regles_figurent_dans_les_consignes(ident, attendus):
    c = consignes(ident)
    manquants = [m for m in attendus if sans_accents(m) not in c]
    assert not manquants, f"{ident} : absent des consignes : {manquants}"


def test_regle_de_synthese_identique_a_celle_de_pixelkreol():
    """Le directeur reprend à l'identique montants, dates et taux, et signale les écarts."""
    c = consignes("orchestrateur")
    for mot in ("montants", "dates", "taux", "a l'identique", "ne les arrondis pas", "signale"):
        assert mot in c


def test_aucune_donnee_ni_secret_dans_instances_abi():
    motifs = re.compile(r"(sk-[A-Za-z0-9]{20,}|gsk_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,})")
    for chemin in ABI.rglob("*"):
        if chemin.is_file():
            assert not motifs.search(chemin.read_text(encoding="utf-8", errors="ignore")), chemin
    assert not (ABI / ".env").exists()


def test_la_fiche_envoyee_au_modele_ne_cite_pas_pixelkreol(client_abi, h_api, faux):
    client_abi.post("/v1/chat/completions", headers=h_api, json={
        "model": "pk-04-appels-de-fonds", "messages": [{"role": "user", "content": "bonjour"}]})
    envoye = sans_accents(str(faux.appels[0]["messages"]))
    for interdit in ("pixelkreol", "la reunion", "will", "kap numerik"):
        assert interdit not in envoye
    assert "date et heure en corse" in envoye
    page = sans_accents(client_abi.get("/").text.split("<main>")[0])
    assert "abi holding" in page and "pixelkreol" not in page


def test_pk_equipe_fonctionne_avec_le_registre_abi(client_abi, h_api, faux):
    import json

    def f(corps):
        user = corps["messages"][-1]["content"]
        if "PLANIFICATION_JSON" in user:
            assert "devis" not in user and "appels-de-fonds" in user       # le plan ne propose que les agents ABI
            return FauxLiteLLM.ok(json.dumps({"taches": [{"agent": "appels-de-fonds", "consigne": "calcul"},
                                                         {"agent": "suivi-programme", "consigne": "jalon"}]}))
        return FauxLiteLLM.ok("réponse")
    faux.par_defaut = f
    r = client_abi.post("/v1/chat/completions", headers=h_api, json={
        "model": "pk-equipe", "messages": [{"role": "user", "content": "Prépare l'appel de fonds hors d'eau"}]})
    assert r.status_code == 200
    texte = r.json()["choices"][0]["message"]["content"]
    assert "Appels de fonds" in texte and "Suivi de programme" in texte


# =============================================================== 4. docker-compose
def compose():
    return yaml.safe_load((ABI / "docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_trois_services_tous_prefixes_abi():
    c = compose()
    assert set(c["services"]) == {"abi_postgres", "abi_orchestrateur", "abi_openwebui"}
    for nom, svc in c["services"].items():
        assert svc["container_name"] == nom and nom.startswith("abi_")
    assert set(c["volumes"]) == {"abi_postgres_data", "abi_openwebui_data"}
    assert all(v["name"].startswith("abi_") for v in c["volumes"].values())
    assert c["networks"]["abi_net"]["name"] == "abi_net"


def test_compose_aucun_port_publie_hors_127_0_0_1():
    c = compose()
    for nom, svc in c["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{nom} publie {port}"
    assert "ports" not in c["services"]["abi_postgres"] and "ports" not in c["services"]["abi_orchestrateur"]
    assert c["services"]["abi_openwebui"]["ports"] == ["127.0.0.1:${ABI_PORT_WEBUI:-3101}:8080"]


def test_compose_limites_memoire():
    s = compose()["services"]
    assert s["abi_orchestrateur"]["mem_limit"] == "256m" and s["abi_postgres"]["mem_limit"] == "256m"
    assert s["abi_openwebui"]["mem_limit"] in ("1g", "1024m")
    assert all("mem_limit" in v for v in s.values())


def test_compose_aucune_image_latest_ou_flottante():
    for nom, svc in compose()["services"].items():
        image = svc["image"]
        defaut = re.search(r"\$\{[A-Z_]+:-([^}]+)\}", image)
        image = defaut.group(1) if defaut else image
        tag = image.rsplit(":", 1)[-1]
        assert ":" in image and re.search(r"\d", tag), f"{nom} : image non épinglée ({image})"
        assert tag not in ("latest", "main", "stable", "dev", "nightly", "main-stable")


def test_compose_reseaux():
    s = compose()["services"]
    assert s["abi_postgres"]["networks"] == ["abi_net"]
    assert s["abi_openwebui"]["networks"] == ["abi_net"]
    assert set(s["abi_orchestrateur"]["networks"]) == {"abi_net", "pixelkreol_net"}
    assert compose()["networks"]["pixelkreol_net"]["external"] is True


def test_compose_orchestrateur_meme_code_agents_en_lecture_seule():
    o = compose()["services"]["abi_orchestrateur"]
    assert o["build"] == "../../orchestrateur" and (ABI / o["build"] / "app.py").resolve() == (RACINE / "orchestrateur" / "app.py")
    assert "./agents:/app/agents:ro" in o["volumes"]
    assert o["environment"]["LITELLM_URL"] == "http://pk_litellm:4000"
    assert o["environment"]["PK_MARQUE"] == "ABI Holding"


def test_compose_openwebui_branche_sur_l_orchestrateur_et_inscriptions_fermees():
    w = compose()["services"]["abi_openwebui"]["environment"]
    assert w["OPENAI_API_BASE_URLS"] == "http://abi_orchestrateur:4001/v1"
    assert w["ENABLE_SIGNUP"] == "${ABI_OPENWEBUI_SIGNUP:-false}"
    assert w["DEFAULT_USER_ROLE"] == "pending" and w["ENABLE_OLLAMA_API"] == "false"
    assert w["ENABLE_PERSISTENT_CONFIG"] == "false"


def test_compose_variables_documentees_dans_env_exemple():
    texte = (ABI / "docker-compose.yml").read_text(encoding="utf-8")
    utilisees = set(re.findall(r"\$\{(ABI_[A-Z_]+)", texte))
    exemple = (ABI / ".env.exemple").read_text(encoding="utf-8")
    declarees = set(re.findall(r"^(ABI_[A-Z_]+)=", exemple, re.M))
    assert utilisees <= declarees, utilisees - declarees


def test_env_exemple_sans_aucune_valeur_et_commente():
    lignes = (ABI / ".env.exemple").read_text(encoding="utf-8").splitlines()
    for i, l in enumerate(lignes):
        m = re.match(r"^([A-Z_]+)=(.*)$", l)
        if m:
            assert m.group(2) == "", f"{m.group(1)} a une valeur"
            precedent = [x for x in lignes[:i] if x.strip()][-1]
            assert precedent.startswith("#") or re.match(r"^[A-Z_]+=$", precedent), f"{m.group(1)} sans commentaire"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker absent")
def test_docker_compose_config_valide():
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp", "ABI_LITELLM_KEY": "x", "ABI_API_KEY": "x",
           "ABI_ADMIN_KEY": "x", "ABI_POSTGRES_PASSWORD": "x", "ABI_WEBUI_SECRET_KEY": "x"}
    r = subprocess.run(["docker", "compose", "-f", str(ABI / "docker-compose.yml"), "config", "-q"], capture_output=True, text=True, env=env)
    if "compose" in r.stderr and "not a docker command" in r.stderr:
        pytest.skip("plugin compose absent")
    assert r.returncode == 0, r.stderr


# =============================================================== 5. nginx et documentation
def test_nginx_modele():
    conf = (ABI / "nginx" / "abi.mebeeltech.online.conf").read_text(encoding="utf-8")
    assert "server_name abi.mebeeltech.online;" in conf
    assert "proxy_pass http://127.0.0.1:3101;" in conf
    assert "proxy_set_header Upgrade $http_upgrade;" in conf and 'proxy_set_header Connection "upgrade";' in conf
    assert re.search(r"client_max_body_size\s+\d+m;", conf)
    assert "proxy_http_version 1.1;" in conf and "proxy_read_timeout" in conf


def test_lisezmoi_abi_complet():
    t = (ABI / "LISEZMOI.md").read_text(encoding="utf-8")
    for mot in ("terminal Hostinger", "docker compose up -d --build", "docker compose stop", "docker compose down",
                "git checkout", "down -v", "certbot --nginx -d abi.mebeeltech.online", "nginx -t", "DNS", "données fictives",
                "ABI_OPENWEBUI_SIGNUP", "ABI_LITELLM_KEY", "docker stats"):
        assert mot in t, mot
    assert t.count("*(terminal Hostinger)*") + t.count("(terminal Hostinger") >= 6


# =============================================================== 6. données fictives
DF = ABI / "donnees-fictives"


def fichiers_df():
    return sorted(DF.glob("*.md"))


def test_chaque_fichier_commence_par_la_mention_fictive():
    assert len(fichiers_df()) >= 9
    for f in fichiers_df():
        assert f.read_text(encoding="utf-8").startswith("DONNÉES FICTIVES — TEST"), f.name


def lignes_table(fichier, titre_colonne):
    """Lignes d'une table markdown dont l'en-tête commence par `titre_colonne`."""
    lignes = (DF / fichier).read_text(encoding="utf-8").splitlines()
    for i, l in enumerate(lignes):
        if l.startswith("| " + titre_colonne):
            rows = []
            for r in lignes[i + 2:]:
                if not r.startswith("|"):
                    break
                rows.append([c.strip() for c in r.strip().strip("|").split("|")])
            return rows
    raise AssertionError(f"table « {titre_colonne} » introuvable dans {fichier}")


def euros(s):
    return D(s.replace("€", "").replace(" ", "").replace(" ", "").replace(",", ".").strip())


def test_acquereurs_noms_manifestement_fictifs():
    rows = lignes_table("acquereurs.md", "N°")
    assert 6 <= len(rows) <= 10
    for r in rows:
        assert re.search(r"FICTIF|EXEMPLE|TEST|INVENTÉ|FACTICE|IMAGINAIRE|BIDON|DÉMO", r[1]), r[1]


def test_echeancier_somme_100_pourcent_et_plafonds_sauf_anomalie_volontaire():
    prix = {r[2]: euros(r[4]) for r in lignes_table("acquereurs.md", "N°")}
    hors_plafond = []
    for r in lignes_table("echeancier-vefa.md", "N° | Lot | Acquéreur"):
        if len(r) != 10:
            continue
        pcts = [int(x.replace("%", "").strip()) for x in r[4:9]]
        assert sum(pcts) == 100 and int(r[9].replace("%", "")) == 100
        cumul = [sum(pcts[:i + 1]) for i in range(5)]
        if cumul[1] > 35 or cumul[2] > 70 or cumul[3] > 95:
            hors_plafond.append(r[1])
        assert euros(r[3]) == prix[r[1]]
    assert hors_plafond == ["B08"]                        # la seule anomalie, volontaire


def test_echeancier_montants_calcules_justes_et_somme_egale_au_prix():
    prix = {r[2]: euros(r[4]) for r in lignes_table("acquereurs.md", "N°")}
    pourcents = {r[1]: [int(x.replace("%", "")) for x in r[4:9]]
                 for r in lignes_table("echeancier-vefa.md", "N° | Lot | Acquéreur") if len(r) == 10}
    montants = {r[1]: [euros(x) for x in r[2:7]] for r in lignes_table("echeancier-vefa.md", "N° | Lot | Signature")
                if len(r) == 7}
    assert set(montants) == set(prix) == set(pourcents)
    for lot, ms in montants.items():
        assert sum(ms) == prix[lot], lot
        for m, p in zip(ms, pourcents[lot]):
            assert m == prix[lot] * p / 100


def test_grille_coherente_avec_les_ventes():
    grille = {r[0]: r for r in lignes_table("programme-et-grille.md", "Lot")}
    assert len(grille) == 20
    acq = {r[2]: euros(r[4]) for r in lignes_table("acquereurs.md", "N°")}
    for lot, p in acq.items():
        assert grille[lot][5] == "vendu" and euros(grille[lot][4]) == p
    assert sum(1 for r in grille.values() if r[5] == "vendu") == len(acq)
    assert sorted(r[0] for r in lignes_table("reservations.md", "Lot")) == sorted(l for l, r in grille.items() if r[5] == "réservé")


def test_trois_baux_et_revision_irl_exacte():
    assert len(lignes_table("baux-et-quittances.md", "Réf.")) == 3
    t = (DF / "baux-et-quittances.md").read_text(encoding="utf-8")
    assert D("650.00") * D("146.16") / D("144.00") == D("659.75") and "659,75 €" in t
    assert "FICTIFS" in t and "quittance" in t.lower()
    septembre = lignes_table("baux-et-quittances.md", "Bail")
    assert [r[0] for r in septembre] == ["BAIL-A", "BAIL-B", "BAIL-C"] and "IMPAYÉ" in septembre[2][3]


def test_mails_planning_reserves_archives_presents():
    assert len(lignes_table("mails-entrants.md", "N°")) >= 8
    assert len(lignes_table("planning-chantier.md", "Lot de travaux")) >= 5
    assert len(lignes_table("planning-chantier.md", "N° | Lot | Pièce")) >= 5
    assert "doublon" in (DF / "index-archives.md").read_text(encoding="utf-8").lower()
    assert "mise hors d'eau" in (DF / "attestations-avancement.md").read_text(encoding="utf-8")


def test_donnees_fictives_ne_citent_pas_pixelkreol_ni_de_vraies_coordonnees():
    for f in fichiers_df():
        t = sans_accents(f.read_text(encoding="utf-8"))
        assert "pixelkreol" not in t and "mebeeltech" not in t, f.name
        assert not re.search(r"\b(?:0|\+33)\s?[1-9](?:[\s.]?\d{2}){4}\b", t), f"numéro de téléphone dans {f.name}"
        assert not re.search(r"\bFR\d{2}[ ]?\d{4}", t.upper()), f"IBAN dans {f.name}"
