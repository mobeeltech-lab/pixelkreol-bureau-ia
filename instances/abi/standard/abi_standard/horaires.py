"""Horaires d'ouverture : servent à dater les rappels et à signaler un message reçu hors horaires (ils ne retardent aucun envoi)."""
from __future__ import annotations

import datetime as dt
import re


def _minutes(hhmm: str) -> int:
    h, m = hhmm.strip().split(":")
    return min(1440, int(h) * 60 + int(m))


def lire_jours(texte: str) -> set[int]:
    jours: set[int] = set()
    for morceau in re.split(r"[,; ]+", texte or ""):
        if re.fullmatch(r"[1-7]", morceau):
            jours.add(int(morceau))
        elif re.fullmatch(r"[1-7]-[1-7]", morceau):
            a, b = int(morceau[0]), int(morceau[2])
            jours |= set(range(a, b + 1)) if a <= b else set(range(a, 8)) | set(range(1, b + 1))
    return jours or {1, 2, 3, 4, 5}


class Horaires:
    def __init__(self, horaires: str = "08:00-18:00", jours: str = "1-5"):
        try:
            a, b = horaires.split("-")
            self.debut, self.fin = _minutes(a), _minutes(b)
        except (ValueError, AttributeError):
            self.debut, self.fin = 8 * 60, 18 * 60
        if self.debut == self.fin:
            self.debut, self.fin = 0, 1440
        self.jours = lire_jours(jours)

    def ouvert(self, t: dt.datetime) -> bool:
        if t.isoweekday() not in self.jours:
            return False
        m = t.hour * 60 + t.minute
        return self.debut <= m < self.fin if self.debut < self.fin else (m >= self.debut or m < self.fin)

    def prochaine_ouverture(self, t: dt.datetime) -> dt.datetime:
        if self.ouvert(t):
            return t
        for d in range(0, 9):
            jour = (t + dt.timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
            if jour.isoweekday() not in self.jours:
                continue
            debut = jour + dt.timedelta(minutes=self.debut % 1440)
            if debut >= t:
                return debut
        return t + dt.timedelta(days=1)

    def echeance(self, t: dt.datetime, heures: int) -> dt.datetime:
        """Délai de rappel : `heures` après la prochaine ouverture, ramené dans les horaires."""
        return self.prochaine_ouverture(self.prochaine_ouverture(t) + dt.timedelta(hours=heures))
