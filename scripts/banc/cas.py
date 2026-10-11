"""Assemble tous les cas d'essai du banc."""
from __future__ import annotations

from . import cas_direction_vente, cas_gestion_relation, cas_nouveaux, cas_production, cas_prompt, cas_prompt_abi, cas_technique_produit
from .regles import Cas


def tous(bureau: str = "pixelkreol") -> list[Cas]:
    """Cas du banc. « abi » : uniquement les 10 cas du Spécialiste Prompt d'ABI Holding (les autres agents d'ABI n'ont pas encore de banc)."""
    if bureau == "abi":
        return list(cas_prompt_abi.CAS)
    liste = [c for m in (cas_direction_vente, cas_production, cas_gestion_relation, cas_technique_produit, cas_nouveaux, cas_prompt) for c in m.CAS]
    vus = set()
    for c in liste:
        if c.cle in vus:
            raise ValueError(f"cas en double : {c.cle}")
        vus.add(c.cle)
    return liste


def par_agent(liste: list[Cas] | None = None) -> dict[str, list[Cas]]:
    sortie: dict[str, list[Cas]] = {}
    for c in liste or tous():
        sortie.setdefault(c.agent, []).append(c)
    return sortie
