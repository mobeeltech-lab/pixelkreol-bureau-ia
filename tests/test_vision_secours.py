"""Cerveau « vision » épuisé : l'agent ne reste plus bloqué.

- une image n'est « active » que dans le DERNIER message de l'utilisateur ;
- les images plus anciennes sont remplacées par un texte avant de partir vers un cerveau texte ;
- si pk-vision échoue (429, 404, 408, 5xx, réponse vide), repli sur la chaîne de l'agent, images retirées, avertissement en tête ;
- 400 / 401 / 403 : pas de nouvel envoi ailleurs.
Aucun vrai fournisseur : LiteLLM est un faux serveur.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from conftest import FauxLiteLLM
from test_abi import abi_dir, app_abi, client_abi  # noqa: F401  (fixtures du registre ABI)
from test_bascule import lire_sse

OK = FauxLiteLLM.ok
ST = FauxLiteLLM.statut
IMG = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
ANCIENNE = "[image envoyée plus tôt dans la conversation — non relue]"
ILLISIBLE = "[image non lue : les modèles de lecture d'images sont indisponibles pour le moment]"
AVERT = "⚠️ Je n'ai pas pu lire l'image (modèles d'images indisponibles pour le moment) : je réponds à partir du texte seulement."


def conversation_ancienne_image():
    return [{"role": "user", "content": [{"type": "text", "text": "Voici ma maquette"}, IMG]},
            {"role": "assistant", "content": "Je la vois : le bouton est trop petit."},
            {"role": "user", "content": "applique les modifications"}]


def conversation_image_recente():
    return [{"role": "user", "content": "Bonjour"}, {"role": "assistant", "content": "Bonjour !"},
            {"role": "user", "content": [{"type": "text", "text": "Que vois-tu ?"}, IMG]}]


def poser(client, h_api, modele, messages, stream=False):
    return client.post("/v1/chat/completions", headers=h_api, json={"model": modele, "stream": stream, "messages": messages})


def a_des_images(corps) -> bool:
    return "image_url" in json.dumps(corps["messages"])


# =============================================================== 1. image ancienne, dernier message texte
def test_image_ancienne_et_question_texte_le_developpeur_garde_pk_code(client, h_api, faux):
    r = poser(client, h_api, "pk-06-developpeur", conversation_ancienne_image())
    assert r.status_code == 200 and faux.modeles_appeles() == ["pk-code"]
    envoye = faux.appels[0]["messages"]
    assert not a_des_images(faux.appels[0])
    assert envoye[1]["content"][0] == {"type": "text", "text": "Voici ma maquette"}      # le reste du message est gardé
    assert envoye[1]["content"][1] == {"type": "text", "text": ANCIENNE}
    assert "AVERT" not in r.text and AVERT not in r.json()["choices"][0]["message"]["content"]


def test_image_ancienne_vision_epuisee_ne_bloque_plus_le_developpeur(client, h_api, faux):
    faux.scenarios["pk-vision"] = [ST(429, "You exceeded your current quota")]
    r = poser(client, h_api, "pk-06-developpeur", conversation_ancienne_image())
    assert r.status_code == 200 and "pk-vision" not in faux.modeles_appeles()


def test_ancien_message_nettoye_puis_images_remplacees_puis_borne(client, h_api, faux):
    msgs = conversation_ancienne_image()
    msgs[0]["reasoning_content"] = "champ propre à un fournisseur"
    msgs[0]["content"].insert(0, {"type": "text", "text": "x" * 30000})
    poser(client, h_api, "pk-06-developpeur", msgs)
    premier = faux.appels[0]["messages"][1]
    assert "reasoning_content" not in premier                                   # nettoyé
    textes = [p["text"] for p in premier["content"]]
    assert ANCIENNE in textes                                                   # remplacé
    assert "caractères retirés" in textes[0] and len(textes[0]) < 25000        # borné


# =============================================================== 2. image dans le dernier message
def test_image_recente_et_vision_ok_comportement_inchange(client, h_api, faux):
    faux.scenarios["pk-vision"] = [OK("Je vois un bouton")]
    r = poser(client, h_api, "pk-06-developpeur", conversation_image_recente())
    assert r.status_code == 200 and faux.modeles_appeles() == ["pk-vision"]
    assert r.json()["choices"][0]["message"]["content"] == "Je vois un bouton"
    assert a_des_images(faux.appels[0])
    assert r.headers["x-pk-cerveau"] == "pk-vision"


@pytest.mark.parametrize("panne", [ST(429, "quota"), ST(404, "absent"), ST(408, "délai"), ST(500, "boum"), ST(503, "off"),
                                   httpx.ReadTimeout("trop long"), httpx.ConnectError("injoignable"), OK("   ")])
def test_vision_en_panne_bascule_sur_le_cerveau_de_l_agent_avec_avertissement(client, h_api, faux, panne):
    faux.scenarios["pk-vision"] = [panne]
    faux.scenarios["pk-code"] = [OK("voici ma réponse")]
    r = poser(client, h_api, "pk-06-developpeur", conversation_image_recente())
    assert r.status_code == 200 and faux.modeles_appeles() == ["pk-vision", "pk-code"]
    contenu = r.json()["choices"][0]["message"]["content"]
    assert contenu.startswith(AVERT) and contenu.endswith("voici ma réponse")
    assert not a_des_images(faux.appels[1])
    assert faux.appels[1]["messages"][-1]["content"][1] == {"type": "text", "text": ILLISIBLE}
    assert faux.appels[1]["messages"][-1]["content"][0] == {"type": "text", "text": "Que vois-tu ?"}
    assert r.headers["x-pk-cerveau"] == "pk-code"


def test_la_reponse_ne_pretend_jamais_avoir_vu_l_image(client, h_api, faux):
    faux.scenarios["pk-vision"] = [ST(429, "quota")]
    contenu = poser(client, h_api, "pk-06-developpeur", conversation_image_recente()).json()["choices"][0]["message"]["content"]
    assert contenu.splitlines()[0] == AVERT


def test_repli_texte_suit_les_secours_de_l_agent(client, h_api, faux):
    faux.scenarios["pk-vision"] = [ST(429, "quota")]
    faux.scenarios["pk-code"] = [ST(503, "off")]
    faux.scenarios["pk-costaud"] = [OK("secours")]
    r = poser(client, h_api, "pk-06-developpeur", conversation_image_recente())
    assert r.status_code == 200 and faux.modeles_appeles() == ["pk-vision", "pk-code", "pk-costaud"]


def test_tout_en_panne_erreur_claire_avec_tous_les_essais(client, h_api, faux):
    faux.par_defaut = ST(503, "off")
    r = poser(client, h_api, "pk-06-developpeur", conversation_image_recente())
    assert r.status_code == 503
    msg = r.json()["error"]["message"]
    assert "pk-vision" in msg and "pk-code" in msg


@pytest.mark.parametrize("statut", [400, 401, 403])
def test_erreur_de_requete_ou_de_cle_ne_renvoie_pas_le_texte_ailleurs(client, h_api, faux, statut):
    """Justification : 400/401/403 = requête refusée ou clé invalide, un problème de configuration. Renvoyer le texte vers un autre cerveau
    cacherait l'erreur ; on garde donc le comportement d'erreur d'avant (aucun nouvel envoi, pas de réponse sans avertissement)."""
    faux.scenarios["pk-vision"] = [ST(statut, "refusé")]
    r = poser(client, h_api, "pk-06-developpeur", conversation_image_recente())
    assert r.status_code == statut and faux.modeles_appeles() == ["pk-vision"]
    assert AVERT not in r.text


def test_agent_a_cerveau_vision_sans_image_utilise_ses_secours(client, h_api, faux):
    faux.scenarios["pk-vision"] = [ST(429, "quota")]
    faux.scenarios["pk-costaud"] = [OK("texte seul")]
    r = poser(client, h_api, "pk-09-designer", [{"role": "user", "content": "Propose une palette"}])
    assert r.status_code == 200 and faux.modeles_appeles() == ["pk-vision", "pk-costaud"]
    assert AVERT not in r.json()["choices"][0]["message"]["content"]        # aucune image à signaler


def test_journal_de_la_bascule_vision_texte(client, h_api, faux, caplog):
    faux.scenarios["pk-vision"] = [ST(429, "quota")]
    with caplog.at_level("WARNING", logger="pk"):
        poser(client, h_api, "pk-06-developpeur", conversation_image_recente())
    lignes = [r.getMessage() for r in caplog.records if "bascule vision -> texte" in r.getMessage()]
    assert len(lignes) == 1
    assert "agent=developpeur" in lignes[0] and "cerveau_origine=pk-vision" in lignes[0] and "cerveau_utilise=pk-code" in lignes[0] and "statut=429" in lignes[0]
    assert "Que vois-tu" not in lignes[0] and "AAAA" not in lignes[0]          # aucun contenu dans le journal


# =============================================================== 3. mode flux
def flux_ok(texte):
    return FauxLiteLLM.flux([FauxLiteLLM.sse(texte), b"data: [DONE]\n\n"])


def test_flux_image_recente_vision_ok(client, h_api, faux):
    faux.scenarios["pk-vision"] = [flux_ok("vu")]
    texte, fin, _ = lire_sse(poser(client, h_api, "pk-06-developpeur", conversation_image_recente(), stream=True))
    assert texte == "vu" and fin and faux.modeles_appeles() == ["pk-vision"]


def test_flux_vision_429_bascule_avec_avertissement_en_tete(client, h_api, faux):
    faux.scenarios["pk-vision"] = [ST(429, "quota")]
    faux.scenarios["pk-code"] = [flux_ok("réponse texte")]
    r = poser(client, h_api, "pk-06-developpeur", conversation_image_recente(), stream=True)
    texte, fin, _ = lire_sse(r)
    assert r.status_code == 200 and fin
    assert texte.startswith(AVERT) and texte.endswith("réponse texte")
    assert faux.modeles_appeles() == ["pk-vision", "pk-code"] and not a_des_images(faux.appels[1])
    assert r.headers["x-pk-cerveau"] == "pk-code"


def test_flux_vision_vide_bascule(client, h_api, faux):
    faux.scenarios["pk-vision"] = [FauxLiteLLM.flux([])]
    faux.scenarios["pk-code"] = [flux_ok("ok")]
    texte, fin, _ = lire_sse(poser(client, h_api, "pk-06-developpeur", conversation_image_recente(), stream=True))
    assert texte.startswith(AVERT) and fin


def test_flux_401_pas_de_bascule(client, h_api, faux):
    faux.scenarios["pk-vision"] = [ST(401, "clé invalide")]
    r = poser(client, h_api, "pk-06-developpeur", conversation_image_recente(), stream=True)
    assert r.status_code == 401 and faux.modeles_appeles() == ["pk-vision"]


def test_flux_image_ancienne_reste_sur_pk_code(client, h_api, faux):
    faux.scenarios["pk-code"] = [flux_ok("modifications appliquées")]
    texte, fin, _ = lire_sse(poser(client, h_api, "pk-06-developpeur", conversation_ancienne_image(), stream=True))
    assert texte == "modifications appliquées" and faux.modeles_appeles() == ["pk-code"]


# =============================================================== 4. redirection automatique vers photo-video
def router(app, faux, messages):
    faux.par_defaut = ST(429, "quota")                                     # IA de routage en panne : mots-clés / défaut seulement
    client = httpx.AsyncClient(transport=httpx.MockTransport(faux))
    return asyncio.run(app.choisir_agent(client, messages))


def test_redirection_photo_video_seulement_pour_une_image_du_dernier_message(appli, faux):
    agent, methode = router(appli, faux, [{"role": "user", "content": [{"type": "text", "text": "hmm"}, IMG]}])
    assert agent["id"] == "photo-video" and methode == "image"
    agent, methode = router(appli, faux, [{"role": "user", "content": [{"type": "text", "text": "hmm"}, IMG]},
                                          {"role": "assistant", "content": "ok"}, {"role": "user", "content": "hmm"}])
    assert agent["id"] != "photo-video" and methode == "défaut"


def test_contient_image_regarde_le_dernier_message_utilisateur(appli):
    assert appli.contient_image(conversation_image_recente()) and not appli.contient_image(conversation_ancienne_image())
    assert not appli.contient_image([]) and not appli.contient_image([{"role": "assistant", "content": [IMG]}])
    apres_outil = conversation_image_recente() + [{"role": "assistant", "content": None, "tool_calls": []}, {"role": "tool", "content": "résultat"}]
    assert appli.contient_image(apres_outil)                                # un résultat d'outil n'efface pas l'image de la demande


# =============================================================== 5. mode équipe
def test_equipe_les_anciennes_images_ne_partent_pas_vers_les_cerveaux_texte(client, h_api, faux):
    faux.par_defaut = OK('{"taches":[{"agent":"devis","consigne":"chiffre"}]}')
    r = poser(client, h_api, "pk-equipe", conversation_ancienne_image() + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "et le devis ?"}])
    assert r.status_code == 200
    assert all(not a_des_images(a) for a in faux.appels) and "pk-vision" not in faux.modeles_appeles()


# =============================================================== 6. ABI : mêmes règles avec le registre d'ABI
def test_abi_image_ancienne_reste_sur_le_cerveau_de_l_agent(client_abi, h_api):
    faux = client_abi.faux
    r = poser(client_abi, h_api, "pk-09-archiviste", conversation_ancienne_image())
    assert r.status_code == 200 and faux.modeles_appeles() == ["pk-long"] and not a_des_images(faux.appels[0])
    assert faux.appels[0]["messages"][1]["content"][1]["text"] == ANCIENNE


def test_abi_vision_429_bascule_avec_avertissement(client_abi, h_api):
    faux = client_abi.faux
    faux.scenarios["pk-vision"] = [ST(429, "quota")]
    faux.scenarios["pk-long"] = [OK("depuis le texte")]
    r = poser(client_abi, h_api, "pk-09-archiviste", conversation_image_recente())
    assert r.status_code == 200 and faux.modeles_appeles() == ["pk-vision", "pk-long"]
    assert r.json()["choices"][0]["message"]["content"] == AVERT + "\n\ndepuis le texte"


def test_abi_vision_ok_inchange_et_401_sans_bascule(client_abi, h_api):
    faux = client_abi.faux
    faux.scenarios["pk-vision"] = [OK("vu")]
    assert poser(client_abi, h_api, "pk-09-archiviste", conversation_image_recente()).json()["choices"][0]["message"]["content"] == "vu"
    faux.appels.clear()
    faux.scenarios["pk-vision"] = [ST(401, "clé")]
    r = poser(client_abi, h_api, "pk-09-archiviste", conversation_image_recente())
    assert r.status_code == 401 and faux.modeles_appeles() == ["pk-vision"]


def test_abi_flux_vision_429(client_abi, h_api):
    faux = client_abi.faux
    faux.scenarios["pk-vision"] = [ST(429, "quota")]
    faux.scenarios["pk-long"] = [flux_ok("réponse")]
    texte, fin, _ = lire_sse(poser(client_abi, h_api, "pk-09-archiviste", conversation_image_recente(), stream=True))
    assert texte.startswith(AVERT) and texte.endswith("réponse") and fin


# =============================================================== 7. DeepSeek retiré des chaînes (solde épuisé : 402 certain à chaque appel, offre payante)
def test_deepseek_n_est_plus_dans_les_chaines():
    import yaml
    from conftest import RACINE
    cerveaux = yaml.safe_load((RACINE / "agents" / "cerveaux.yaml").read_text(encoding="utf-8"))
    assert "deepseek" not in cerveaux["fournisseurs"]
    assert all(m["f"] != "deepseek" for c in cerveaux["cerveaux"].values() for m in c["chaine"])
    config = yaml.safe_load((RACINE / "config" / "litellm_config.yaml").read_text(encoding="utf-8"))
    assert not any("deepseek" in str(m["litellm_params"]) for m in config["model_list"])
    noms = {m["model_name"] for m in config["model_list"]}
    assert all(x in noms for f in config["router_settings"]["fallbacks"] for v in f.values() for x in v)     # aucun secours orphelin


# =============================================================== 8. ordre des cerveaux d'images et de documents longs (13/10/2026)
def ordre_litellm(cerveau):
    import yaml
    from conftest import RACINE
    c = yaml.safe_load((RACINE / "config" / "litellm_config.yaml").read_text(encoding="utf-8"))
    modeles = {m["model_name"]: m["litellm_params"]["model"] for m in c["model_list"]}
    secours = {k: v for f in c["router_settings"]["fallbacks"] for k, v in f.items()}
    return [modeles[n] for n in [cerveau] + secours[cerveau]]


def ordre_yaml(cerveau):
    import yaml
    from conftest import RACINE
    c = yaml.safe_load((RACINE / "agents" / "cerveaux.yaml").read_text(encoding="utf-8"))
    return [f"{m['f']}/{m['modele']}" for m in c["cerveaux"][cerveau]["chaine"]]


def test_pk_vision_mistral_puis_gemini_lite_puis_gemini_flash():
    attendu = ["mistral/mistral-small-latest", "gemini/gemini-flash-lite-latest", "gemini/gemini-flash-latest"]
    assert ordre_yaml("pk-vision")[:3] == attendu
    assert ordre_litellm("pk-vision")[:3] == attendu


def test_pk_long_gemini_lite_avant_flash():
    attendu = ["gemini/gemini-flash-lite-latest", "gemini/gemini-flash-latest"]
    assert ordre_yaml("pk-long")[:2] == attendu
    assert ordre_litellm("pk-long")[:2] == attendu
