#!/usr/bin/env python3
"""Met à jour les connexions OpenAI enregistrées dans la base d'Open WebUI.

Open WebUI mémorise ses connexions dans sa base au premier démarrage et ignore
ensuite les variables d'environnement. Ce script ajoute/rafraîchit les connexions
du Bureau IA (orchestrateur + LiteLLM) SANS toucher au reste des réglages
(utilisateurs, modèles personnalisés, prompts, historique).

Deux formats de base sont gérés :
  * récent (2026) : table `config(key, value, updated_at)`, une ligne par réglage ;
  * ancien        : table `config(id, data, ...)`, un seul JSON.

Usage : python3 patch_openwebui.py /chemin/webui.db URL1 CLE1 [URL2 CLE2 ...]
Code retour : 0 = modifié, 3 = rien à faire (base absente ou jamais configurée).
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

NOS_HOTES = ("pk_orchestrateur", "pk_litellm", "pk_routing_proxy")
COPIES_GARDEES = 3  # nombre de copies « avant » conservées à côté de la base (les plus anciennes sont retirées)


def sauvegarder(db: Path) -> Path:
    """Copie cohérente de la base AVANT toute modification (retour arrière : remettre cette copie en place)."""
    cible = db.with_name(f"{db.name}.avant-{time.strftime('%Y%m%d-%H%M%S')}")
    source, copie = sqlite3.connect(db), sqlite3.connect(cible)
    try:
        source.backup(copie)
    finally:
        copie.close()
        source.close()
    os.chmod(cible, 0o600)
    anciennes = sorted(db.parent.glob(db.name + ".avant-*"))
    for vieille in anciennes[:-COPIES_GARDEES]:
        vieille.unlink()
    print(f"Copie de sécurité de la base Open WebUI : {cible.name}")
    return cible


def fusion(urls, cles, configs, paires):
    cles = list(cles) + [""] * (len(urls) - len(cles))
    gardes = [(u, k, (configs or {}).get(str(i), {})) for i, (u, k) in enumerate(zip(urls, cles))
              if not any(h in u for h in NOS_HOTES)]
    nouvelles = [(u, k, {"enable": True}) for u, k in paires] + gardes
    return ([u for u, _, _ in nouvelles], [k for _, k, _ in nouvelles],
            {str(i): c for i, (_, _, c) in enumerate(nouvelles)}, len(gardes))


def main() -> int:
    if len(sys.argv) < 4 or (len(sys.argv) - 2) % 2:
        print(__doc__)
        return 1
    db = Path(sys.argv[1])
    paires = list(zip(sys.argv[2::2], sys.argv[3::2]))
    if not db.exists():
        print("Base Open WebUI absente : premier démarrage, les variables d'environnement suffisent.")
        return 3
    con = sqlite3.connect(db)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "config" not in tables:
            print("Table config absente : rien à modifier.")
            return 3
        colonnes = [r[1] for r in con.execute("PRAGMA table_info(config)")]

        if "key" in colonnes and "value" in colonnes:  # format récent clé/valeur
            lire = {k: v for k, v in con.execute("SELECT key, value FROM config WHERE key LIKE 'openai.%'")}
            if not lire:
                print("Aucune connexion enregistrée : les variables d'environnement seront utilisées.")
                return 3

            def j(k, defaut):
                try:
                    return json.loads(lire[k]) if k in lire and lire[k] is not None else defaut
                except (TypeError, json.JSONDecodeError):
                    print(f"Avertissement : le réglage « {k} » est illisible, une valeur vide est utilisée à la place "
                          "(l'ancienne valeur reste dans la copie de sécurité).", file=sys.stderr)
                    return defaut
            urls, cles, cfgs, n = fusion(j("openai.api_base_urls", []), j("openai.api_keys", []),
                                         j("openai.api_configs", {}), paires)
            sauvegarder(db)
            maintenant = int(time.time())
            for k, v in (("openai.enable", True), ("openai.api_base_urls", urls),
                         ("openai.api_keys", cles), ("openai.api_configs", cfgs)):
                if con.execute("UPDATE config SET value=?, updated_at=? WHERE key=?",
                               (json.dumps(v), maintenant, k)).rowcount == 0:
                    con.execute("INSERT INTO config(key, value, updated_at) VALUES(?,?,?)",
                                (k, json.dumps(v), maintenant))
        else:  # ancien format : un JSON unique
            ligne = con.execute("SELECT id, data FROM config ORDER BY id DESC LIMIT 1").fetchone()
            if ligne is None:
                print("Configuration vide : les variables d'environnement seront utilisées.")
                return 3
            cid, brut = ligne
            sauvegarder(db)
            data = json.loads(brut) if isinstance(brut, str) else brut
            o = data.setdefault("openai", {})
            o["api_base_urls"], o["api_keys"], o["api_configs"], n = fusion(
                o.get("api_base_urls") or [], o.get("api_keys") or [], o.get("api_configs") or {}, paires)
            o["enable"] = True
            con.execute("UPDATE config SET data=? WHERE id=?", (json.dumps(data), cid))
        con.commit()
        print(f"Connexions Open WebUI : {len(paires)} du Bureau IA + {n} autre(s) conservée(s).")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
