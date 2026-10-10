#!/usr/bin/env python3
"""Génère config/litellm_config.yaml à partir de agents/cerveaux.yaml.

Règles :
  * un fournisseur n'est inclus que si sa clé existe (non vide) dans l'environnement
    ou dans le fichier .env passé avec --env ;
  * chaque cerveau devient une chaîne LiteLLM : `pk-rapide` (1er fournisseur dispo)
    puis `pk-rapide--2`, `pk-rapide--3`… déclarés en fallbacks, sans retry inutile,
    pour qu'une erreur 429 bascule immédiatement sur le suivant ;
  * un cerveau sans aucun fournisseur disponible est signalé (code retour 2 si
    AUCUN cerveau n'est utilisable).

Usage : python3 outils/generer_litellm.py [--cerveaux agents/cerveaux.yaml]
        [--env .env] [--sortie config/litellm_config.yaml] [--rapport]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

RACINE = Path(__file__).resolve().parent.parent


def lire_env(chemin: Path | None) -> dict[str, str]:
    """Valeurs du fichier .env, surchargées par les variables d'environnement réelles."""
    env: dict[str, str] = {}
    if chemin and chemin.exists():
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#") or "=" not in ligne:
                continue
            k, v = ligne.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if v})
    return env


def construire(cerveaux_cfg: dict, env: dict[str, str]) -> tuple[dict, dict[str, list[str]]]:
    fournisseurs = cerveaux_cfg["fournisseurs"]
    model_list: list[dict] = []
    fallbacks: list[dict] = []
    rapport: dict[str, list[str]] = {}

    for cerveau, spec in cerveaux_cfg["cerveaux"].items():
        noms: list[str] = []
        vus: set[str] = set()
        for maillon in spec["chaine"]:
            f = fournisseurs.get(maillon["f"])
            if f is None:
                raise SystemExit(f"Fournisseur inconnu '{maillon['f']}' dans le cerveau {cerveau}")
            cle = f["cle"]
            if not env.get(cle, "").strip():
                continue
            modele = f"{f['prefixe']}/{maillon['modele']}"
            if modele in vus:
                continue
            vus.add(modele)
            nom = cerveau if not noms else f"{cerveau}--{len(noms) + 1}"
            params: dict = {"model": modele, "api_key": f"os.environ/{cle}"}
            if f.get("api_base"):
                params["api_base"] = f["api_base"]
            if maillon.get("max_tokens"):
                params["max_tokens"] = maillon["max_tokens"]
            model_list.append({
                "model_name": nom,
                "litellm_params": params,
                "model_info": {"description": f"{cerveau} via {maillon['f']}"},
            })
            noms.append(nom)
        rapport[cerveau] = [m["litellm_params"]["model"] for m in model_list
                            if m["model_name"] == cerveau or m["model_name"].startswith(cerveau + "--")]
        if len(noms) > 1:
            fallbacks.append({noms[0]: noms[1:]})

    config = {
        "model_list": model_list,
        "router_settings": {
            "fallbacks": fallbacks,
            "num_retries": 0,
            "allowed_fails": 1,
            "cooldown_time": 45,
            "timeout": 90,
            "enable_pre_call_checks": False,
        },
        "litellm_settings": {
            # drop_params : retire les paramètres qu'un fournisseur ne supporte pas
            # (ex. `user`, `seed`). Sans danger ici : c'est le proxy qui construit
            # la requête finale (l'ancien bug Content-Length venait du proxy maison).
            "drop_params": True,
            "request_timeout": 90,
            "num_retries": 0,
            "set_verbose": False,
        },
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
        },
    }
    return config, rapport


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cerveaux", default=str(RACINE / "agents" / "cerveaux.yaml"))
    p.add_argument("--env", default=str(RACINE / ".env"))
    p.add_argument("--sortie", default=str(RACINE / "config" / "litellm_config.yaml"))
    p.add_argument("--rapport", action="store_true")
    a = p.parse_args()

    cfg = yaml.safe_load(Path(a.cerveaux).read_text(encoding="utf-8"))
    env = lire_env(Path(a.env))
    config, rapport = construire(cfg, env)

    entete = ("# FICHIER GÉNÉRÉ par outils/generer_litellm.py — ne pas modifier à la main.\n"
              "# Pour changer les modèles : éditer agents/cerveaux.yaml puis relancer install.sh\n")
    Path(a.sortie).parent.mkdir(parents=True, exist_ok=True)
    Path(a.sortie).write_text(entete + yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
                              encoding="utf-8")

    utilisables = {c: m for c, m in rapport.items() if m}
    if True:  # rapport toujours affiché (lisible dans les logs d'installation)
        for c, modeles in rapport.items():
            etat = f"{len(modeles)} fournisseur(s)" if modeles else "AUCUN fournisseur (clé manquante)"
            print(f"  {c:<11} {etat}" + (f"  → {modeles[0]}" if modeles else ""))
    if not utilisables:
        print("ERREUR : aucune clé API de fournisseur n'est renseignée dans .env", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
