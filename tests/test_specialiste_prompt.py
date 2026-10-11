"""Spécialiste Prompt : fiche chargée dans les deux bureaux, annuaire généré (et seulement pour lui), routage, modèles de demandes, règles du banc.

Aucun vrai modèle : LiteLLM est un faux serveur.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from banc import cas as cas_mod  # noqa: E402
from banc.regles import montants, noter  # noqa: E402
from cas_routage import CAS as CAS_PK  # noqa: E402
from cas_routage_abi import CAS as CAS_ABI  # noqa: E402
from conftest import CLE_API, RACINE, FauxLiteLLM, charger_app  # noqa: E402

ABI = RACINE / "instances" / "abi"
ID = "specialiste-prompt"
TITRE_ANNUAIRE = "## Annuaire des agents"


def registre(chemin: Path) -> list[dict]:
    return yaml.safe_load(chemin.read_text(encoding="utf-8"))["agents"]


@pytest.fixture
def app_abi(monkeypatch, tmp_path):
    dest = tmp_path / "agents_abi"
    shutil.copytree(ABI / "agents", dest)
    return charger_app(monkeypatch, dest, PK_MARQUE="ABI Holding", PK_PROPRIETAIRE="abi")


def message_systeme(faux) -> str:
    return next(m["content"] for m in faux.appels[-1]["messages"] if m["role"] == "system")


def poser(client, modele, texte="bonjour"):
    h = {"Authorization": f"Bearer {CLE_API}"}
    return client.post("/v1/chat/completions", headers=h, json={"model": modele, "messages": [{"role": "user", "content": texte}]})


# =============================================================== 1. la fiche, dans les deux bureaux
def test_fiche_pixelkreol_numero_34_et_champs():
    a = registre(RACINE / "agents" / "registre.yaml")[-1]
    assert (a["id"], a["num"], a["famille"], a["cerveau"], a["temperature"]) == (ID, 34, "produit", "pk-costaud", 0.3)
    assert a["inclure_annuaire"] is True and "outils" not in a
    assert len(a["consignes"]) <= 2900
    for mot in ("[SECRET RETIRÉ]", "au plus 3 questions", "5 ingrédients", "Ce que j'ai ajouté", "ORANGE", "pk-equipe", "pk-25-fabrique-agents", "JAMAIS", "grille"):
        assert mot in a["consignes"], mot


def test_fiche_abi_numero_10_sans_outil_et_sans_donnee_reelle():
    a = registre(ABI / "agents" / "registre.yaml")[-1]
    assert (a["id"], a["num"], a["cerveau"], a["temperature"]) == (ID, 10, "pk-costaud", 0.3)
    assert a["inclure_annuaire"] is True
    assert "outils" not in a                                              # ni NAS ni MCP : il travaille sur le texte donné
    assert re.search(r"\bVous\b", a["consignes"]) and "Will" not in a["consignes"]       # vouvoiement, pas de nom propre de PixelKréol
    for mot in ("[SECRET RETIRÉ]", "au plus 3 questions", "5 ingrédients", "ORANGE", "pk-equipe", "aucun outil"):
        assert mot in a["consignes"], mot


@pytest.mark.parametrize("bureau,nb", [("pixelkreol", 34), ("abi", 10)])
def test_agent_charge_et_compteur_de_sante(bureau, nb, monkeypatch, tmp_path, faux):
    source = RACINE / "agents" if bureau == "pixelkreol" else ABI / "agents"
    dest = tmp_path / "agents"
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns("*.md"))
    app = charger_app(monkeypatch, dest)
    with TestClient(app.app, raise_server_exceptions=False) as tc:
        tc.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
        assert tc.get("/health").json()["agents"] == nb and not app.REGISTRE.avertissements
        a = app.REGISTRE.agents[ID]
        assert a["modele"] == f"pk-{nb:02d}-{ID}"
        assert poser(tc, a["modele"]).headers["x-pk-agent"] == ID


# =============================================================== 2. annuaire : seulement pour lui, généré, borné
@pytest.mark.parametrize("bureau", ["pixelkreol", "abi"])
def test_annuaire_seulement_pour_le_specialiste_prompt(bureau, appli, app_abi):
    app = appli if bureau == "pixelkreol" else app_abi
    for a in app.REGISTRE.agents.values():
        texte = app.REGISTRE.prompt_systeme(a)
        if a["id"] == ID:
            assert TITRE_ANNUAIRE in texte
        else:
            assert TITRE_ANNUAIRE not in texte, a["id"]
    # et il ne l'a pas écrit en dur : aucun nom pk-NN-id dans sa fiche, hors exemples de forme
    assert not re.search(r"pk-\d\d-", app.REGISTRE.agents[ID]["consignes"].replace("pk-04-devis", "").replace("pk-04-appels-de-fonds", "").replace("pk-25-fabrique-agents", ""))


@pytest.mark.parametrize("bureau", ["pixelkreol", "abi"])
def test_annuaire_liste_chaque_agent_avec_son_nom_exact(bureau, appli, app_abi):
    app = appli if bureau == "pixelkreol" else app_abi
    texte = app.REGISTRE.prompt_systeme(app.REGISTRE.agents[ID])
    annuaire = texte[texte.index(TITRE_ANNUAIRE):]
    for a in app.REGISTRE.agents.values():
        if a["id"] == ID:
            assert f"- {a['modele']} :" not in annuaire                       # il ne se recommande pas lui-même
        else:
            assert f"- {a['modele']} : " in annuaire, a["id"]
    assert len(annuaire) <= app.ANNUAIRE_MAX_CAR + 400


def test_annuaire_inclut_les_agents_de_perso_yaml_et_suit_le_rechargement(appli, agents_dir):
    perso = {"agents": [{"id": "garage", "num": 150, "nom": "Garage", "cerveau": "pk-rapide", "description": "Répond aux clients d'un garage automobile.",
                         "consignes": "Tu réponds aux clients d'un garage automobile."}]}
    (agents_dir / "perso.yaml").write_text(yaml.safe_dump(perso, allow_unicode=True), encoding="utf-8")
    appli.REGISTRE.recharger(force=True)
    texte = appli.REGISTRE.prompt_systeme(appli.REGISTRE.agents[ID])
    assert "- pk-150-garage : Répond aux clients d'un garage automobile." in texte
    assert TITRE_ANNUAIRE not in appli.REGISTRE.prompt_systeme(appli.REGISTRE.agents["garage"])      # l'agent du client ne l'a pas


def test_annuaire_borne_en_taille_meme_avec_beaucoup_d_agents(appli, agents_dir):
    longs = [{"id": f"a{i}", "num": 200 + i, "nom": f"A{i}", "cerveau": "pk-rapide", "description": "mot " * 400, "consignes": "Tu aides."} for i in range(120)]
    (agents_dir / "perso.yaml").write_text(yaml.safe_dump({"agents": longs}, allow_unicode=True), encoding="utf-8")
    appli.REGISTRE.recharger(force=True)
    texte = appli.REGISTRE.prompt_systeme(appli.REGISTRE.agents[ID])
    annuaire = texte[texte.index(TITRE_ANNUAIRE):]
    assert len(annuaire) <= appli.ANNUAIRE_MAX_CAR + 400 and "liste tronquée" in annuaire
    assert all(len(l) <= appli.ANNUAIRE_MAX_DESC + 40 for l in annuaire.splitlines()[2:-1])


def test_champ_inclure_annuaire_doit_etre_booleen(appli, agents_dir):
    mauvais = {"agents": [{"id": "x1", "num": 160, "nom": "X", "cerveau": "pk-rapide", "description": "d", "consignes": "c", "inclure_annuaire": "oui"}]}
    (agents_dir / "perso.yaml").write_text(yaml.safe_dump(mauvais), encoding="utf-8")
    appli.REGISTRE.recharger(force=True)
    assert "x1" not in appli.REGISTRE.agents and any("inclure_annuaire" in w for w in appli.REGISTRE.avertissements)


def test_annuaire_reellement_envoye_au_modele_pour_lui_seul(client, faux, h_api):
    assert poser(client, "pk-34-specialiste-prompt", "améliore ma demande : fais le site").status_code == 200
    assert "- pk-04-devis : " in message_systeme(faux) and TITRE_ANNUAIRE in message_systeme(faux)
    poser(client, "pk-04-devis", "bonjour")
    assert TITRE_ANNUAIRE not in message_systeme(faux)


# =============================================================== 3. routage : 10 demandes pour lui, 10 voisines qui n'y vont pas
def route(app, faux, texte):
    faux.par_defaut = FauxLiteLLM.statut(429, "quota")                   # IA de routage en panne : mots-clés seuls (cas le plus défavorable)
    client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
    return asyncio.run(app.choisir_agent(client, [{"role": "user", "content": texte}]))[0]["id"]


@pytest.mark.parametrize("nom,cas,fixture", [("pixelkreol", CAS_PK, "appli"), ("abi", CAS_ABI, "app_abi")])
def test_dix_demandes_vont_au_specialiste_et_dix_voisines_non(nom, cas, fixture, request, faux):
    app = request.getfixturevalue(fixture)
    pour_lui = [t for t, a in cas if a == ID]
    voisines = [(t, a) for t, a in cas if a != ID][-10:]
    assert len(pour_lui) == 10 and len(voisines) == 10
    assert [t for t in pour_lui if route(app, faux, t) != ID] == []
    for t, attendu in voisines:
        obtenu = route(app, faux, t)
        assert obtenu != ID and obtenu == attendu, (t, obtenu)


@pytest.mark.parametrize("texte", ["crée un agent pour un garage", "fais-moi un devis pour le site", "relis ce devis avant envoi au client", "prépare un pack d'agents IA pour un cabinet"])
def test_le_specialiste_ne_vole_pas_les_demandes_voisines(texte, appli, faux):
    assert route(appli, faux, texte) != ID


@pytest.mark.parametrize("texte", ["Améliore ma demande : rédige la mise en demeure pour Chez Fictif", "Écris-moi le prompt pour une relance de facture",
                                   "Reformule ma demande : planning du chantier"])
def test_la_formule_explicite_l_emporte_sur_les_mots_du_metier(texte, appli, faux):
    assert route(appli, faux, texte) == ID


# =============================================================== 4. modèles de demandes
def lire_modeles(chemin: Path):
    texte = chemin.read_text(encoding="utf-8")
    blocs = re.split(r"-{20,}\n", texte)[1:]
    return texte, blocs


@pytest.mark.parametrize("chemin,nb,registre_yaml", [(RACINE / "docs" / "modeles-de-demandes.txt", 15, RACINE / "agents" / "registre.yaml"),
                                                     (ABI / "modeles-de-demandes.txt", 10, ABI / "agents" / "registre.yaml")])
def test_modeles_de_demandes_agents_reels_sans_prix_ni_secret(chemin, nb, registre_yaml):
    texte, blocs = lire_modeles(chemin)
    reels = {f"pk-{a['num']:02d}-{a['id']}" for a in registre(registre_yaml)} | {"pk-equipe"}
    assert len(blocs) == nb
    for b in blocs:
        titre = b.splitlines()[0]
        agent = re.search(r"^Agent recommandé : (\S+)$", b, re.M).group(1)
        assert agent in reels, (titre, agent)
        assert b.count("```") == 2 and "Contexte :" in b and "Tâche :" in b and "Forme du résultat :" in b and "Limites :" in b and "[" in b, titre
    assert montants(texte) == []                                          # aucun prix écrit : ils viennent de la grille via l'agent Devis
    assert not re.search(r"(?i)sk-[a-z0-9]{8,}|mot de passe\s*:|api[_ ]?key\s*[:=]", texte)
    assert "n'a pas pu être vérifié" in texte                             # aucun fichier d'import : dit franchement


def test_modeles_abi_ne_citent_ni_pixelkreol_ni_will():
    texte = (ABI / "modeles-de-demandes.txt").read_text(encoding="utf-8").lower()
    assert "pixelkreol" not in texte and "will" not in texte and "974" not in texte


# =============================================================== 5. règles du banc (pas de modèle : réponses écrites à la main)
def cas(cle, bureau="pixelkreol"):
    return next(c for c in cas_mod.tous(bureau) if c.agent == ID and c.ident == cle)


BONNE_DEVIS = """Agent recommandé : pk-04-devis — c'est lui qui chiffre un site vitrine.

```
Contexte : je suis Will, j'écris pour le salon Chez Fictif à Saint-Denis.
Tâche : chiffrer un site vitrine de 5 pages avec la grille de PixelKréol.
Ce que je te donne : le besoin du client ; [liste des pages] ; liste d'abord ce qui te manque.
Forme du résultat : devis poste par poste, total HT, échéancier.
Limites : aucune remise ; livraison en mars ; Feu : ORANGE, je valide avant l'envoi.
```
Ce que j'ai ajouté : la forme du devis et le feu. Il reste à compléter [budget du client]."""


def rate(note, mot):
    """Vrai si une règle dont le libellé contient `mot` a échoué."""
    return any(mot in e for e in note.echecs)


def test_le_banc_accepte_une_bonne_reponse_et_refuse_les_mauvaises():
    c = cas("devis-clair")
    assert noter(c, BONNE_DEVIS).score >= 0.99
    assert rate(noter(c, BONNE_DEVIS.replace("pk-04-devis", "pk-99-devis-magique")), "agent")                 # agent inventé
    assert rate(noter(c, BONNE_DEVIS.replace("pk-04-devis", "devis")), "au bon format")                       # pas au format pk-NN-id
    assert rate(noter(c, BONNE_DEVIS.replace("```", "")), "bloc de copie")                                    # pas de bloc de copie
    assert rate(noter(c, BONNE_DEVIS.replace("Limites :", "Reste :")), "5 ingrédients")                       # 5e ingrédient absent
    assert rate(noter(c, BONNE_DEVIS + "\nTotal : 1 500 € HT"), "ne contient pas")                                    # prix donné au lieu de laisser le devis à l'agent
    assert rate(noter(c, re.sub(r"\[[^\]]+\]", "à voir", BONNE_DEVIS)), "crochets")                 # donnée manquante sans crochet
    assert rate(noter(c, BONNE_DEVIS.replace("Ce que j'ai ajouté", "Voilà")), "ce que j.ai ajoute")           # ligne « Ce que j'ai ajouté » absente


def test_le_banc_controle_les_questions_le_secret_et_le_feu_orange():
    flou = cas("brouillon-flou")
    assert noter(flou, "1. Quel objectif ? (défaut : vitrine) 2. Quel budget ? (défaut : à voir) 3. Quelle date ? (défaut : mars)").score >= 0.99
    assert rate(noter(flou, "1. A ? 2. B ? 3. C ? 4. D ?"), "au plus 3")                                      # plus de 3 questions
    secret = cas("secret")
    ok = ("Agent recommandé : pk-20-devops. Mot de passe : [SECRET RETIRÉ] — changez-le. Clé : [SECRET RETIRÉ]. ```\nContexte : serveur ; Tâche : régler le certificat ; "
          "Ce que je te donne : [logs] ; Forme : liste ; Limites : Feu ORANGE\n```")
    assert noter(secret, ok).score >= 0.99
    assert rate(noter(secret, ok.replace("[SECRET RETIRÉ]", "Fictif-Mdp-2026!")), "ne contient pas")           # secret recopié
    agent = cas("ameliorer-agent")
    fiche = ("Fiche corrigée : tu vouvoies, l'envoi est ORANGE, la mise en demeure est ROUGE et part chez le Juriste. Changements : ... Questions de test : 1) ... 2) ... 3) ...\n"
             "Feu : ORANGE — à valider par Will")
    assert noter(agent, fiche).score >= 0.99
    assert rate(noter(agent, fiche.replace("Feu : ORANGE — à valider par Will", "")), "Feu : ORANGE")         # pas de feu orange en mode « améliorer un agent »
    plusieurs = cas("plusieurs-metiers")
    assert rate(noter(plusieurs, "Agent recommandé : pk-04-devis. ```" + "mot " * 20 + "```"), "agent recommandé")  # un seul agent au lieu de pk-equipe


def test_le_banc_abi_existe_avec_10_cas_et_les_memes_regles():
    abi = cas_mod.tous("abi")
    assert len(abi) == 10 and {c.agent for c in abi} == {ID} and len({c.cle for c in abi}) == 10
    assert len([c for c in cas_mod.tous() if c.agent == ID]) == 10
    bonne = BONNE_DEVIS.replace("pk-04-devis", "pk-04-appels-de-fonds").replace("PixelKréol", "ABI Holding")
    assert not rate(noter(cas("appel-de-fonds", "abi"), bonne), "agent")
    assert rate(noter(cas("appel-de-fonds", "abi"), bonne.replace("pk-04-appels-de-fonds", "pk-04-devis")), "agent")      # le Devis n'existe pas chez ABI
