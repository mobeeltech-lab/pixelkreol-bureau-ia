#!/usr/bin/env python3
"""Recalcule scripts/banc/SHA256SUMS (empreintes des fichiers à copier sur le serveur). À lancer depuis la racine du dépôt après toute modification."""
import hashlib
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
FICHIERS = ["agents/registre.yaml", "scripts/banc_agents.py", "scripts/banc/__init__.py", "scripts/banc/regles.py", "scripts/banc/cas.py",
            "scripts/banc/cas_direction_vente.py", "scripts/banc/cas_production.py", "scripts/banc/cas_gestion_relation.py",
            "scripts/banc/cas_technique_produit.py", "scripts/banc/runner.py", "scripts/banc/audit.py", "scripts/banc/LISEZMOI.md", "scripts/banc/DEPLOIEMENT.md"]

if __name__ == "__main__":
    lignes = [f"{hashlib.sha256((RACINE / f).read_bytes()).hexdigest()}  {f}" for f in FICHIERS]
    (RACINE / "scripts/banc/SHA256SUMS").write_text("\n".join(lignes) + "\n", encoding="utf-8")
    print("\n".join(lignes))
