"""
title: PK — Calculs (TVA Réunion, Kap Numérik, échéances, devises)
author: PixelKréol
description: Calculs fiables pour devis et factures : TVA DOM, simulation Kap Numérik, dates d'échéance, conversion de devises.
version: 1.0.0
"""

from datetime import date, timedelta
import calendar

import requests
from pydantic import BaseModel, Field

TAUX = {"reunion": 8.5, "reunion_reduit": 2.1, "metropole": 20.0, "metropole_intermediaire": 10.0, "metropole_reduit": 5.5, "exonere": 0.0}


class Tools:
    class Valves(BaseModel):
        KAP_TAUX_0_9: float = Field(default=80.0, description="Kap Numérik : taux de prise en charge 0-9 salariés (%).")
        KAP_PLAFOND_0_9: float = Field(default=3200.0, description="Kap Numérik : plafond d'aide 0-9 salariés (€).")
        KAP_TAUX_10_19: float = Field(default=50.0, description="Kap Numérik : taux 10-19 salariés (%).")
        KAP_PLAFOND_10_19: float = Field(default=2000.0, description="Kap Numérik : plafond d'aide 10-19 salariés (€).")

    def __init__(self):
        self.valves = self.Valves()

    @staticmethod
    def _e(x: float) -> str:
        return f"{x:,.2f} €".replace(",", " ").replace(".", ",")

    def calcul_tva(self, montant: float, sens: str = "ht_vers_ttc", zone: str = "reunion") -> str:
        """
        Calcule HT, TVA et TTC sans erreur. Zones : reunion (8,5 %), reunion_reduit (2,1 %), metropole (20 %), metropole_intermediaire (10 %), metropole_reduit (5,5 %), exonere (0 %).
        :param montant: Montant de départ en euros.
        :param sens: "ht_vers_ttc" ou "ttc_vers_ht".
        :param zone: Zone ou taux de TVA (voir la liste).
        :return: Détail HT / TVA / TTC.
        """
        t = TAUX.get(zone)
        if t is None:
            return f"Zone inconnue. Choix : {', '.join(TAUX)}"
        if sens == "ttc_vers_ht":
            ht = round(montant / (1 + t / 100), 2)
            ttc = round(montant, 2)
        else:
            ht = round(montant, 2)
            ttc = round(ht * (1 + t / 100), 2)
        return f"HT {self._e(ht)} · TVA {str(t).replace('.', ',')} % = {self._e(round(ttc - ht, 2))} · TTC {self._e(ttc)}"

    def total_devis(self, lignes: str, zone: str = "reunion", acompte_pourcent: float = 30.0) -> str:
        """
        Calcule un devis complet à partir de lignes "désignation ; quantité ; prix unitaire HT", une par ligne.
        :param lignes: Lignes séparées par des retours à la ligne, champs séparés par ";" (ex. "Site vitrine ; 1 ; 1200").
        :param zone: Zone de TVA (reunion par défaut).
        :param acompte_pourcent: Pourcentage d'acompte à la commande.
        :return: Tableau des lignes, totaux HT/TVA/TTC et acompte.
        """
        t = TAUX.get(zone, 8.5)
        rows, total = [], 0.0
        for l in lignes.strip().splitlines():
            parts = [p.strip() for p in l.split(";")]
            if len(parts) < 3:
                continue
            q = float(parts[1].replace(",", "."))
            pu = float(parts[2].replace(" ", "").replace(",", "."))
            tot = round(q * pu, 2)
            total += tot
            rows.append(f"| {parts[0]} | {q:g} | {self._e(pu)} | {self._e(tot)} |")
        tva = round(total * t / 100, 2)
        ttc = round(total + tva, 2)
        return ("| Désignation | Qté | PU HT | Total HT |\n|---|---|---|---|\n" + "\n".join(rows) +
                f"\n\nTotal HT {self._e(total)} · TVA {str(t).replace('.', ',')} % {self._e(tva)} · **Total TTC {self._e(ttc)}**"
                f"\nAcompte {acompte_pourcent:g} % : {self._e(round(ttc * acompte_pourcent / 100, 2))} TTC")

    def simulation_kap_numerik(self, montant_ht: float, effectif: int) -> str:
        """
        Estime l'aide Kap Numérik (Région Réunion) et le reste à charge pour un projet numérique.
        :param montant_ht: Montant HT du projet (devis PixelKréol).
        :param effectif: Nombre de salariés de l'entreprise cliente.
        :return: Aide estimée, reste à charge et conditions.
        """
        if effectif <= 9:
            taux, plafond = self.valves.KAP_TAUX_0_9, self.valves.KAP_PLAFOND_0_9
        elif effectif <= 19:
            taux, plafond = self.valves.KAP_TAUX_10_19, self.valves.KAP_PLAFOND_10_19
        else:
            return "Non éligible : Kap Numérik vise les TPE de moins de 20 salariés."
        aide = min(montant_ht * taux / 100, plafond)
        return (f"Projet {self._e(montant_ht)} HT · effectif {effectif} → aide estimée {self._e(aide)} "
                f"({taux:g} % plafonné à {self._e(plafond)}) · reste à charge {self._e(montant_ht - aide)} HT.\n"
                "Conditions : siège à La Réunion, inscription RCS/RM, projet non démarré avant le dépôt, un dossier par gérant et par an. "
                "Exclus : secteur numérique, professions réglementées, agriculture, pêche. Estimation sous réserve d'acceptation du dossier ; montants à revérifier chaque année.")

    def calcul_echeance(self, date_facture: str, delai_jours: int = 30, fin_de_mois: bool = False) -> str:
        """
        Calcule une date d'échéance de paiement (le délai légal maximum entre professionnels est 60 jours, ou 45 jours fin de mois).
        :param date_facture: Date au format AAAA-MM-JJ.
        :param delai_jours: Délai de paiement en jours.
        :param fin_de_mois: True pour "X jours fin de mois".
        :return: La date d'échéance.
        """
        d = date.fromisoformat(date_facture)
        if fin_de_mois:
            d = d + timedelta(days=delai_jours)
            d = d.replace(day=calendar.monthrange(d.year, d.month)[1])
        else:
            d = d + timedelta(days=delai_jours)
        return f"Échéance : {d.strftime('%d/%m/%Y')}" + (" (au-delà du plafond légal de 60 jours)" if delai_jours > 60 else "")

    def convertir_devise(self, montant: float, de: str = "EUR", vers: str = "USD") -> str:
        """
        Convertit un montant avec le taux de change du jour (Banque centrale européenne). Codes ISO : EUR, USD, GBP, ZAR, CHF, CNY, JPY, AUD, CAD...
        :param montant: Montant à convertir.
        :param de: Devise de départ.
        :param vers: Devise d'arrivée.
        :return: Montant converti et date du taux.
        """
        r = requests.get("https://api.frankfurter.dev/v1/latest", params={"base": de.upper(), "symbols": vers.upper()}, timeout=20)
        r.raise_for_status()
        j = r.json()
        taux = j["rates"][vers.upper()]
        return f"{montant:g} {de.upper()} = {montant * taux:,.2f} {vers.upper()} (taux BCE du {j['date']} : {taux})".replace(",", " ")
