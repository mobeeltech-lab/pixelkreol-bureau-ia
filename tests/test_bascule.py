"""C. Bascule entre cerveaux : 429, 5xx, délai, réponse vide, flux coupé."""
from __future__ import annotations

import json

import httpx
import pytest

from conftest import FauxLiteLLM

OK = FauxLiteLLM.ok
ST = FauxLiteLLM.statut


def chat(client, h_api, modele="pk-12-secretariat", stream=False, texte="Écris juste : bonjour"):
    return client.post("/v1/chat/completions", headers=h_api,
                       json={"model": modele, "stream": stream, "messages": [{"role": "user", "content": texte}]})


# secretariat = pk-rapide ; secours : pk-rapide -> [pk-costaud]
@pytest.mark.parametrize("panne", [
    ST(429, "quota dépassé"), ST(500, "boum"), ST(502, "bad gateway"), ST(503, "indisponible"),
    ST(404, "modèle introuvable"), ST(408, "délai"), httpx.ReadTimeout("trop long"),
    httpx.ConnectError("injoignable"),
])
def test_bascule_sur_le_cerveau_de_secours(client, h_api, faux, panne):
    faux.scenarios["pk-rapide"] = [panne]
    faux.scenarios["pk-costaud"] = [OK("depuis le secours")]
    r = chat(client, h_api)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "depuis le secours"
    assert r.headers["x-pk-cerveau"] == "pk-costaud"
    assert faux.modeles_appeles() == ["pk-rapide", "pk-costaud"]  # un seul essai chacun : pas de double appel


def test_un_appel_reussi_n_est_facture_qu_une_fois(client, h_api, faux):
    r = chat(client, h_api)
    assert r.status_code == 200 and faux.modeles_appeles() == ["pk-rapide"]


def test_reponse_vide_bascule_sur_le_secours(client, h_api, faux):
    faux.scenarios["pk-rapide"] = [OK("")]
    faux.scenarios["pk-costaud"] = [OK("plein")]
    r = chat(client, h_api)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "plein"


def test_reponse_vide_acceptee_si_appel_d_outil(client, h_api, faux):
    corps = {"id": "x", "object": "chat.completion", "created": 1, "model": "m", "choices": [
        {"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}}]}
    faux.scenarios["pk-rapide"] = [httpx.Response(200, json=corps)]
    r = chat(client, h_api)
    assert r.status_code == 200 and faux.modeles_appeles() == ["pk-rapide"]


def test_reponse_200_pas_json_bascule_au_lieu_de_planter(client, h_api, faux):
    faux.scenarios["pk-rapide"] = [httpx.Response(200, text="<html>passerelle</html>")]
    faux.scenarios["pk-costaud"] = [OK("secours")]
    r = chat(client, h_api)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "secours"


def test_toute_la_chaine_en_panne_message_clair_sans_boucle(client, h_api, faux):
    faux.par_defaut = ST(429, "quota")
    r = chat(client, h_api)
    assert r.status_code in (429, 502, 503)
    assert len(faux.appels) == 2  # pk-rapide puis pk-costaud, puis on s'arrête
    err = r.json()["error"]
    assert "Aucun cerveau disponible" in err["message"]
    assert "pk-rapide" in err["message"] and "pk-costaud" in err["message"]  # dit ce qui a été essayé
    assert err["type"] == "cerveaux_indisponibles"


def test_erreur_client_400_ne_bascule_pas(client, h_api, faux):
    faux.scenarios["pk-rapide"] = [ST(400, "requête invalide")]
    r = chat(client, h_api)
    assert r.status_code == 400 and faux.modeles_appeles() == ["pk-rapide"]


def test_les_secrets_renvoyes_par_le_fournisseur_sont_masques(client, h_api, faux):
    faux_secret = "sk" + "-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3"      # fabriqué ici : aucune vraie clé dans le dépôt
    faux.par_defaut = ST(401, f"Incorrect API key provided: {faux_secret} / Bearer cle-litellm-de-test")
    r = chat(client, h_api)
    assert faux_secret not in r.text
    assert "cle-litellm-de-test" not in r.text


def test_les_secrets_ne_sont_pas_dans_les_journaux(client, h_api, faux, caplog):
    groq = "gsk" + "_" + "a1b2c3d4e5f6g7h8i9j0k1l2m3"
    faux.par_defaut = ST(500, f"oups Bearer cle-litellm-de-test {groq}")
    with caplog.at_level("DEBUG"):
        chat(client, h_api)
        chat(client, {"Authorization": "Bearer mauvaise-cle"})
    texte = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("cle-litellm-de-test", "cle-api-de-test", groq, "mauvaise-cle"):
        assert secret not in texte


def test_vision_ne_bascule_pas_vers_un_cerveau_texte(client, h_api, faux):
    faux.par_defaut = ST(503, "off")
    r = client.post("/v1/chat/completions", headers=h_api, json={"model": "pk-09-designer", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "?"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]})
    assert r.status_code >= 400 and set(faux.modeles_appeles()) == {"pk-vision"}


# ---------------------------------------------------------------- flux (streaming)
def lire_sse(r):
    texte, fin, evts = "", False, []
    for ligne in r.text.splitlines():
        if ligne.startswith("data: "):
            if ligne == "data: [DONE]":
                fin = True
                continue
            o = json.loads(ligne[6:])
            evts.append(o)
            for c in o.get("choices", []):
                texte += (c.get("delta") or {}).get("content") or ""
    return texte, fin, evts


def test_flux_bascule_avant_le_premier_octet(client, h_api, faux):
    faux.scenarios["pk-rapide"] = [ST(429, "quota")]
    faux.scenarios["pk-costaud"] = [FauxLiteLLM.flux([FauxLiteLLM.sse("sal"), FauxLiteLLM.sse("ut"), b"data: [DONE]\n\n"])]
    r = chat(client, h_api, stream=True)
    texte, fin, _ = lire_sse(r)
    assert r.status_code == 200 and texte == "salut" and fin
    assert faux.modeles_appeles() == ["pk-rapide", "pk-costaud"]


def test_flux_coupe_au_milieu_previent_l_utilisateur(client, h_api, faux, caplog):
    faux.scenarios["pk-rapide"] = [FauxLiteLLM.flux([FauxLiteLLM.sse("Bonjour, voici le d")], coupe=True)]
    with caplog.at_level("WARNING", logger="pk"):
        r = chat(client, h_api, stream=True)
    texte, fin, evts = lire_sse(r)
    assert texte.startswith("Bonjour, voici le d")
    assert "interrompue" in texte.lower()          # message clair, pas un texte coupé en silence
    assert fin                                      # le flux est proprement terminé
    assert faux.modeles_appeles() == ["pk-rapide"]  # pas de second appel (pas de double facturation)
    assert evts[-1]["choices"][0]["finish_reason"] is not None
    assert any("flux interrompu" in rec.getMessage() for rec in caplog.records)


def test_flux_termine_sans_done_est_signale(client, h_api, faux):
    faux.scenarios["pk-rapide"] = [FauxLiteLLM.flux([FauxLiteLLM.sse("début")])]  # fin propre mais sans [DONE]
    texte, fin, _ = lire_sse(chat(client, h_api, stream=True))
    assert "interrompue" in texte.lower() and fin


def test_flux_vide_bascule_sur_le_secours(client, h_api, faux):
    faux.scenarios["pk-rapide"] = [FauxLiteLLM.flux([])]
    faux.scenarios["pk-costaud"] = [FauxLiteLLM.flux([FauxLiteLLM.sse("ok"), b"data: [DONE]\n\n"])]
    texte, fin, _ = lire_sse(chat(client, h_api, stream=True))
    assert texte == "ok" and fin and faux.modeles_appeles() == ["pk-rapide", "pk-costaud"]


def test_flux_toute_la_chaine_en_panne(client, h_api, faux):
    faux.par_defaut = ST(503, "off")
    r = chat(client, h_api, stream=True)
    assert r.status_code >= 400 and len(faux.appels) == 2
    assert "Aucun cerveau disponible" in r.json()["error"]["message"]


def test_flux_erreur_dans_le_premier_bloc_bascule(client, h_api, faux):
    err = b'data: {"error": {"message": "rate limit", "code": 429}}\n\n'
    faux.scenarios["pk-rapide"] = [FauxLiteLLM.flux([err])]
    faux.scenarios["pk-costaud"] = [FauxLiteLLM.flux([FauxLiteLLM.sse("bon"), b"data: [DONE]\n\n"])]
    texte, fin, _ = lire_sse(chat(client, h_api, stream=True))
    assert texte == "bon"
