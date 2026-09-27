"""
title: PK — La Réunion pratique (heure, jours fériés, météo, adresses)
author: PixelKréol
description: Heure Réunion/Paris, jours fériés et jours ouvrés à La Réunion, météo par commune, géocodage d'adresses (sans clé API).
version: 1.0.0
"""

from datetime import date, datetime, timedelta, timezone

import requests
from pydantic import BaseModel

RE = timezone(timedelta(hours=4))
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
WMO = {0: "ciel clair", 1: "peu nuageux", 2: "partiellement nuageux", 3: "couvert", 45: "brouillard", 48: "brouillard givrant",
       51: "bruine", 53: "bruine", 55: "bruine forte", 61: "pluie faible", 63: "pluie", 65: "pluie forte", 80: "averses",
       81: "averses", 82: "averses violentes", 95: "orage", 96: "orage avec grêle", 99: "orage fort"}


class Tools:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    def _feries(self, annee: int) -> dict:
        r = requests.get(f"https://calendrier.api.gouv.fr/jours-feries/la-reunion/{annee}.json", timeout=20)
        r.raise_for_status()
        return r.json()

    def date_heure(self) -> str:
        """
        Donne la date et l'heure actuelles à La Réunion et à Paris (pour dater un document ou caler un rendez-vous avec la métropole).
        :return: Date et heures.
        """
        now = datetime.now(RE)
        try:
            from zoneinfo import ZoneInfo
            paris = datetime.now(ZoneInfo("Europe/Paris"))
        except Exception:
            paris = now - timedelta(hours=2)
        return f"La Réunion : {JOURS[now.weekday()]} {now.strftime('%d/%m/%Y %H:%M')} (UTC+4) · Paris : {paris.strftime('%d/%m/%Y %H:%M')}"

    def jours_feries(self, annee: int = 0) -> str:
        """
        Liste les jours fériés à La Réunion pour une année (dont le 20 décembre, abolition de l'esclavage).
        :param annee: Année (0 = année en cours).
        :return: Liste des jours fériés.
        """
        a = annee or datetime.now(RE).year
        return "\n".join(f"- {JOURS[date.fromisoformat(d).weekday()]} {date.fromisoformat(d).strftime('%d/%m/%Y')} : {n}" for d, n in self._feries(a).items())

    def jours_ouvres(self, date_debut: str, date_fin: str) -> str:
        """
        Compte les jours ouvrés (lundi-vendredi hors jours fériés réunionnais) entre deux dates incluses, pour un planning ou un délai de livraison.
        :param date_debut: Date AAAA-MM-JJ.
        :param date_fin: Date AAAA-MM-JJ.
        :return: Nombre de jours ouvrés et fériés rencontrés.
        """
        d1, d2 = date.fromisoformat(date_debut), date.fromisoformat(date_fin)
        fer = {}
        for a in range(d1.year, d2.year + 1):
            fer.update(self._feries(a))
        n, rencontres, d = 0, [], d1
        while d <= d2:
            if d.isoformat() in fer:
                rencontres.append(f"{d.strftime('%d/%m')} {fer[d.isoformat()]}")
            elif d.weekday() < 5:
                n += 1
            d += timedelta(days=1)
        return f"{n} jours ouvrés du {d1.strftime('%d/%m/%Y')} au {d2.strftime('%d/%m/%Y')}" + (f" (fériés : {', '.join(rencontres)})" if rencontres else "")

    def geocoder_adresse(self, adresse: str) -> str:
        """
        Vérifie et normalise une adresse française (Base Adresse Nationale) et donne ses coordonnées GPS.
        :param adresse: Adresse à vérifier (ex. "12 rue Jean Chatel Saint-Denis").
        :return: Adresse normalisée, code postal, commune et coordonnées.
        """
        r = requests.get("https://data.geopf.fr/geocodage/search", params={"q": adresse, "limit": 5}, timeout=20)
        r.raise_for_status()
        feats = sorted(r.json().get("features", []), key=lambda f: not str(f["properties"].get("postcode", "")).startswith("974"))
        out = []
        for f in feats:
            p, (lon, lat) = f["properties"], f["geometry"]["coordinates"]
            out.append(f"- {p.get('label')} (fiabilité {p.get('score', 0):.0%}) · GPS {lat:.5f}, {lon:.5f}")
        return "\n".join(out) or "Adresse introuvable."

    def meteo(self, commune: str = "Saint-Denis", jours: int = 3) -> str:
        """
        Prévisions météo pour une commune de La Réunion (planifier une séance photo/vidéo, un chantier, un événement). Pour les alertes cycloniques, se référer à Météo-France La Réunion.
        :param commune: Nom de la commune (ex. "Saint-Pierre", "Cilaos").
        :param jours: Nombre de jours (1 à 7).
        :return: Prévisions jour par jour.
        """
        g = requests.get("https://data.geopf.fr/geocodage/search", params={"q": commune + " La Réunion", "type": "municipality", "limit": 1}, timeout=20).json()
        if not g.get("features"):
            return "Commune introuvable."
        lon, lat = g["features"][0]["geometry"]["coordinates"]
        m = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "timezone": "Indian/Reunion", "forecast_days": max(1, min(int(jours), 7)),
            "daily": "weather_code,temperature_2m_min,temperature_2m_max,precipitation_sum,precipitation_probability_max,wind_speed_10m_max"}, timeout=40).json()["daily"]
        out = [f"Météo {g['features'][0]['properties'].get('city', commune)} :"]
        for i, d in enumerate(m["time"]):
            out.append(f"- {JOURS[date.fromisoformat(d).weekday()]} {d[8:]}/{d[5:7]} : {WMO.get(m['weather_code'][i], 'variable')}, "
                       f"{m['temperature_2m_min'][i]:.0f}-{m['temperature_2m_max'][i]:.0f} °C, pluie {m['precipitation_sum'][i]} mm "
                       f"({m['precipitation_probability_max'][i]} %), vent max {m['wind_speed_10m_max'][i]:.0f} km/h")
        return "\n".join(out)
