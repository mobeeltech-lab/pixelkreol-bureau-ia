"""Stockage SQLite : numérotation continue des pièces, registre des factures, fichiers, mails, sites."""
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config

_lock = threading.Lock()
DB = config.DATA / "atelier.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS compteurs (serie TEXT PRIMARY KEY, valeur INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pieces (
  numero TEXT PRIMARY KEY, type TEXT NOT NULL, date TEXT NOT NULL, client TEXT NOT NULL,
  total_ht REAL, total_tva REAL, total_ttc REAL, statut TEXT NOT NULL, donnees TEXT NOT NULL,
  fichier TEXT, ref TEXT, cree_le TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fichiers (
  jeton TEXT PRIMARY KEY, nom TEXT NOT NULL, chemin TEXT NOT NULL, type TEXT, taille INTEGER, cree_le TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mails (
  id TEXT PRIMARY KEY, a TEXT NOT NULL, cc TEXT, sujet TEXT NOT NULL, corps TEXT NOT NULL,
  pieces TEXT, statut TEXT NOT NULL, agent TEXT, cree_le TEXT NOT NULL, envoye_le TEXT, erreur TEXT);
CREATE TABLE IF NOT EXISTS sites (
  slug TEXT PRIMARY KEY, nom TEXT NOT NULL, expire_le TEXT, mot_de_passe TEXT, cree_le TEXT NOT NULL, taille INTEGER);
CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY AUTOINCREMENT, quand TEXT, action TEXT, detail TEXT);
"""


def maintenant() -> datetime:
    return datetime.now(ZoneInfo(config.FUSEAU))


@contextmanager
def conn():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


MIGRATIONS = [
    "ALTER TABLE sites ADD COLUMN production INTEGER DEFAULT 0",
    "ALTER TABLE sites ADD COLUMN domaine TEXT",
    "ALTER TABLE sites ADD COLUMN demande TEXT",
]


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(sites)")}
        for m in MIGRATIONS:
            if m.split("ADD COLUMN ")[1].split()[0] not in cols:
                c.execute(m)


def journal(action: str, detail: dict | str = ""):
    with conn() as c:
        c.execute("INSERT INTO journal(quand, action, detail) VALUES (?,?,?)",
                  (maintenant().isoformat(timespec="seconds"), action,
                   detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)))


def prochain_numero(prefixe: str) -> str:
    """Numérotation continue et chronologique par série et par année : F-2026-0001, D-2026-0001, A-2026-0001."""
    annee = maintenant().year
    serie = f"{prefixe}-{annee}"
    with _lock, conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT valeur FROM compteurs WHERE serie=?", (serie,)).fetchone()
        n = (row["valeur"] if row else 0) + 1
        c.execute("INSERT INTO compteurs(serie, valeur) VALUES(?,?) ON CONFLICT(serie) DO UPDATE SET valeur=excluded.valeur",
                  (serie, n))
    return f"{serie}-{n:04d}"


def ajouter_fichier(nom: str, chemin: str, type_mime: str, taille: int) -> str:
    jeton = secrets.token_urlsafe(18)
    with conn() as c:
        c.execute("INSERT INTO fichiers VALUES (?,?,?,?,?,?)",
                  (jeton, nom, chemin, type_mime, taille, maintenant().isoformat(timespec="seconds")))
    return jeton


def fichier(jeton: str):
    with conn() as c:
        return c.execute("SELECT * FROM fichiers WHERE jeton=?", (jeton,)).fetchone()
