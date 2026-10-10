"""Vérification du Bureau IA (exécuté DANS le conteneur pk_orchestrateur par verifier.sh).

Usage : python verifier.py [--rapide]
  --rapide : pas d'appel aux vrais modèles (seulement santé + catalogue).
Code retour : 0 si le bureau répond (au moins un cerveau opérationnel), 1 sinon.
"""
import json
import os
import sys
import time

import httpx

RAPIDE = "--rapide" in sys.argv
CLE = os.environ.get("LITELLM_MASTER_KEY", "")
CLE_API = os.environ.get("PK_API_KEY") or CLE
LITELLM = os.environ.get("LITELLM_URL", "http://pk_litellm:4000")
ORCH = "http://127.0.0.1:4001"
V, R, J, N = "\033[0;32m", "\033[0;31m", "\033[1;33m", "\033[0m"
bilan = {"ok": 0, "ko": 0}


def ligne(ok, texte, detail=""):
    bilan["ok" if ok else "ko"] += 1
    print(f"  {V + '✔' if ok else R + '✘'}{N} {texte}" + (f"  {J}{detail}{N}" if detail else ""))


def main() -> int:
    c = httpx.Client(timeout=60)
    h_llm = {"Authorization": f"Bearer {CLE}"}
    h_api = {"Authorization": f"Bearer {CLE_API}"}

    # 1. santé
    try:
        s = c.get(f"{ORCH}/health").json()
        ligne(s["ok"], f"Orchestrateur v{s['version']} : {s['agents']} agents chargés",
              "; ".join(s["avertissements"]))
        ligne(s["litellm"], "LiteLLM joignable depuis l'orchestrateur")
    except Exception as e:  # noqa: BLE001
        ligne(False, "Orchestrateur injoignable", str(e))
        return 1

    modeles = c.get(f"{ORCH}/v1/models", headers=h_api).json()["data"]
    ligne(len(modeles) == s["agents"] + 2, f"Catalogue : {len(modeles)} modèles (pk-auto, pk-equipe + agents)")

    deployments = []
    if not deployments:
        try:
            info = c.get(f"{LITELLM}/v1/models", headers=h_llm).json()["data"]
            deployments = [m["id"] for m in info]
        except Exception as e:  # noqa: BLE001
            ligne(False, "Liste des cerveaux LiteLLM", str(e))
    cerveaux = sorted({d.split("--")[0] for d in deployments})
    ligne(bool(cerveaux), f"Cerveaux déclarés : {', '.join(cerveaux)} ({len(deployments)} fournisseurs au total)")

    if RAPIDE:
        return 0 if bilan["ko"] == 0 else 1

    # 2. chaque fournisseur, un par un (sans bascule) — pour savoir lesquels répondent vraiment
    print("\n  Fournisseurs (appel réel, 1 mot) :")
    marche = {}
    for d in deployments:
        t = time.monotonic()
        try:
            r = c.post(f"{LITELLM}/v1/chat/completions", headers=h_llm, timeout=45, json={
                "model": d, "max_tokens": 8, "temperature": 0,
                "messages": [{"role": "user", "content": "Réponds seulement : OK"}],
                "disable_fallbacks": True})
            ok = r.status_code == 200  # 200 = clé valide et modèle accessible (le texte peut être vide pour
            # les modèles « à raisonnement » quand max_tokens est petit)
            detail = f"{int((time.monotonic() - t) * 1000)} ms" if ok else f"HTTP {r.status_code} {r.text[:90]}"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)[:90]
        marche[d] = ok
        print(f"    {V + '✔' if ok else J + '·'}{N} {d:<16} {detail}")
    for cv in cerveaux:
        dispo = [d for d in deployments if d.split("--")[0] == cv and marche.get(d)]
        ligne(bool(dispo), f"Cerveau {cv} : {len(dispo)} fournisseur(s) opérationnel(s)",
              "" if dispo else "aucun — l'orchestrateur basculera sur un autre cerveau")

    # 3. agents de bout en bout
    print()
    essais = [("pk-12-secretariat", "Écris juste le mot : bonjour", False),
              ("pk-auto", "Rédige un post Instagram de 2 lignes pour une boulangerie de Saint-Denis", False),
              ("pk-06-developpeur", "Donne une ligne de Python qui affiche 42", True)]
    for modele, question, flux in essais:
        t = time.monotonic()
        try:
            if flux:
                texte = ""
                with c.stream("POST", f"{ORCH}/v1/chat/completions", headers=h_api, timeout=90, json={
                        "model": modele, "stream": True, "max_tokens": 120,
                        "messages": [{"role": "user", "content": question}]}) as r:
                    agent, cerveau = r.headers.get("x-pk-agent"), r.headers.get("x-pk-cerveau")
                    ok = r.status_code == 200
                    for l in r.iter_lines():
                        if l.startswith("data: ") and l != "data: [DONE]":
                            texte += json.loads(l[6:])["choices"][0]["delta"].get("content") or ""
            else:
                r = c.post(f"{ORCH}/v1/chat/completions", headers=h_api, timeout=90, json={
                    "model": modele, "max_tokens": 200, "messages": [{"role": "user", "content": question}]})
                ok = r.status_code == 200
                agent, cerveau = r.headers.get("x-pk-agent"), r.headers.get("x-pk-cerveau")
                texte = r.json()["choices"][0]["message"]["content"] if ok else r.text
            ok = ok and bool(texte.strip())
            apercu = " ".join(texte.split())[:70]
            ligne(ok, f"{modele}{' (flux)' if flux else ''} → agent {agent} via {cerveau} "
                      f"en {time.monotonic() - t:.1f} s", apercu if ok else texte[:120])
        except Exception as e:  # noqa: BLE001
            ligne(False, f"{modele}", str(e)[:120])

    print(f"\n  Résultat : {bilan['ok']} OK, {bilan['ko']} à regarder")
    return 0 if bilan["ko"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
