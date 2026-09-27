"""
title: PK — Entreprises françaises (SIRENE)
author: PixelKréol
description: Rechercher des entreprises (nom, SIREN, activité, commune, 974) via l'API publique Recherche d'entreprises de l'État, sans clé API.
version: 1.0.0
"""

import requests
from pydantic import BaseModel, Field

API = "https://recherche-entreprises.api.gouv.fr"
EFFECTIFS = {"NN": "non renseigné", "00": "0 salarié", "01": "1-2", "02": "3-5", "03": "6-9", "11": "10-19", "12": "20-49",
             "21": "50-99", "22": "100-199", "31": "200-249", "32": "250-499", "41": "500-999", "42": "1000-1999"}


class Tools:
    class Valves(BaseModel):
        DEPARTEMENT_DEFAUT: str = Field(default="974", description="Département utilisé si aucun lieu n'est précisé (vide = toute la France).")

    def __init__(self):
        self.valves = self.Valves()

    def _search(self, params: dict) -> list:
        r = requests.get(f"{API}/search", params=params, headers={"User-Agent": "pixelkreol-bureau-ia"}, timeout=25)
        if r.status_code == 429:
            raise RuntimeError("Trop de requêtes vers l'annuaire (7 par seconde maximum) : réessayer dans quelques secondes.")
        r.raise_for_status()
        return r.json().get("results", [])

    @staticmethod
    def _fmt(e: dict, detail: bool = False) -> str:
        s = e.get("siege") or {}
        eff = EFFECTIFS.get(e.get("tranche_effectif_salarie") or "NN", e.get("tranche_effectif_salarie") or "?")
        etat = "active" if e.get("etat_administratif") == "A" else "cessée"
        out = (f"- {e.get('nom_complet')} — SIREN {e.get('siren')} ({etat})\n"
               f"  Activité {e.get('activite_principale') or '?'} · créée le {e.get('date_creation') or '?'} · effectif {eff} · "
               f"{e.get('nombre_etablissements_ouverts', '?')} établissement(s)\n  Siège : {s.get('adresse') or '?'} (SIRET {s.get('siret') or '?'})")
        if detail:
            dirs = []
            for d in e.get("dirigeants") or []:
                nom = d.get("denomination") or " ".join(x for x in [d.get("prenoms"), d.get("nom")] if x)
                dirs.append(f"{nom} ({d.get('qualite') or d.get('type_dirigeant') or ''})".strip())
            if dirs:
                out += "\n  Dirigeants : " + "; ".join(dirs[:6])
            comp = e.get("complements") or {}
            flags = [k.replace("est_", "") for k, v in comp.items() if k.startswith("est_") and v is True]
            if flags:
                out += "\n  Particularités : " + ", ".join(flags)
            fin = e.get("finances") or {}
            if fin:
                an = sorted(fin)[-1]
                out += f"\n  Finances {an} : CA {fin[an].get('ca')} € · résultat net {fin[an].get('resultat_net')} €"
        return out

    def rechercher_entreprise(self, nom: str, commune_ou_code_postal: str = "", nombre: int = 5) -> str:
        """
        Trouve une entreprise par son nom ou son SIREN/SIRET (vérifier un prospect, compléter un devis ou une facture).
        :param nom: Nom de l'entreprise, SIREN (9 chiffres) ou SIRET (14 chiffres).
        :param commune_ou_code_postal: Code postal (ex. "97410") pour affiner, facultatif.
        :param nombre: Nombre de résultats (1 à 25).
        :return: Fiches entreprises (SIREN, activité, adresse, effectif, dirigeants).
        """
        p = {"q": nom, "per_page": max(1, min(int(nombre), 25))}
        cp = commune_ou_code_postal.strip()
        if cp.isdigit() and len(cp) == 5:
            p["code_postal"] = cp
        elif not nom.replace(" ", "").isdigit() and self.valves.DEPARTEMENT_DEFAUT:
            p["departement"] = self.valves.DEPARTEMENT_DEFAUT
        res = self._search(p)
        return "\n".join(self._fmt(e, detail=True) for e in res) or "Aucune entreprise trouvée."

    def trouver_prospects(self, activite: str, code_postal: str = "", code_naf: str = "", nombre: int = 15) -> str:
        """
        Liste des entreprises actives d'un secteur dans une zone (prospection commerciale locale, par défaut La Réunion).
        :param activite: Mots-clés du métier (ex. "plombier", "restaurant", "agence immobilière").
        :param code_postal: Code postal pour cibler une commune (ex. "97400"), facultatif.
        :param code_naf: Code NAF précis (ex. "43.22A"), facultatif.
        :param nombre: Nombre de résultats (1 à 25).
        :return: Liste d'entreprises avec adresse et effectif.
        """
        p = {"q": activite, "per_page": max(1, min(int(nombre), 25)), "etat_administratif": "A"}
        if code_postal:
            p["code_postal"] = code_postal
        elif self.valves.DEPARTEMENT_DEFAUT:
            p["departement"] = self.valves.DEPARTEMENT_DEFAUT
        if code_naf:
            p["activite_principale"] = code_naf
        res = self._search(p)
        return (f"{len(res)} entreprise(s) :\n" + "\n".join(self._fmt(e) for e in res)) if res else "Aucune entreprise trouvée."
