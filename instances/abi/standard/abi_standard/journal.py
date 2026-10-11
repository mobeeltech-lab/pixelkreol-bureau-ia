"""Journal et tâches (SQLite, un seul fichier dans le volume de données).

Deux niveaux de sensibilité :
  * `messages` (le JOURNAL) : date, numéro MASQUÉ, catégorie, urgence, destinataires, statut. Aucun nom, aucun texte. Gardé longtemps.
  * `contenus` et `taches` : transcription, synthèse, numéro à rappeler. Supprimés après `conservation_jours` (30 par défaut).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cle TEXT NOT NULL UNIQUE, recu_le TEXT NOT NULL, traite_le TEXT,
  numero_masque TEXT NOT NULL DEFAULT 'inconnu', categorie TEXT, urgence INTEGER, destinataires TEXT NOT NULL DEFAULT '',
  statut TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', duree_audio REAL, secondes_calcul REAL, tronque INTEGER NOT NULL DEFAULT 0,
  classement TEXT, notifie_le TEXT, tentatives INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_messages_statut ON messages(statut);
CREATE TABLE IF NOT EXISTS contenus (
  message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  transcription TEXT NOT NULL DEFAULT '', synthese TEXT NOT NULL DEFAULT '{}', sujet TEXT NOT NULL DEFAULT '', corps TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS taches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  destinataire TEXT NOT NULL, echeance TEXT NOT NULL, libelle TEXT NOT NULL, numero_rappel TEXT NOT NULL DEFAULT '',
  statut TEXT NOT NULL DEFAULT 'a_faire');
CREATE TABLE IF NOT EXISTS meta (cle TEXT PRIMARY KEY, valeur TEXT NOT NULL);
"""


def maintenant_iso(t: dt.datetime | None = None) -> str:
    return (t or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Journal:
    def __init__(self, chemin: Path):
        self.chemin = Path(chemin)
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        with self._cx() as cx:
            cx.executescript(SCHEMA)
        try:
            self.chemin.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _cx(self):
        """Une transaction courte : validée en sortie normale, annulée sur erreur, connexion toujours fermée."""
        cx = sqlite3.connect(self.chemin, timeout=30)
        cx.row_factory = sqlite3.Row
        cx.execute("PRAGMA foreign_keys=ON")
        try:
            yield cx
            cx.commit()
        except BaseException:
            cx.rollback()
            raise
        finally:
            cx.close()

    # ------------------------------------------------------------------ messages
    def connu(self, cle: str) -> bool:
        with self._cx() as cx:
            return cx.execute("SELECT 1 FROM messages WHERE cle=?", (cle,)).fetchone() is not None

    def ouvrir(self, cle: str, recu_le: str, numero_masque: str = "inconnu") -> int | None:
        """Réserve la ligne du message ; None si déjà vue (aucun double traitement, même si le marquage « lu » a échoué)."""
        with self._cx() as cx:
            try:
                cur = cx.execute("INSERT INTO messages(cle,recu_le,numero_masque,statut) VALUES (?,?,?,'en_cours')",
                                 (cle, recu_le, numero_masque))
            except sqlite3.IntegrityError:
                return None
            return cur.lastrowid

    def oublier(self, mid: int) -> None:
        """Retire un message dont le traitement n'a pas pu aller au bout (panne du moteur) : il sera repris tel quel."""
        with self._cx() as cx:
            cx.execute("DELETE FROM messages WHERE id=?", (mid,))

    def mettre_a_jour(self, mid: int, **champs) -> None:
        if not champs:
            return
        noms = ", ".join(f"{k}=?" for k in champs)
        with self._cx() as cx:
            cx.execute(f"UPDATE messages SET {noms} WHERE id=?", (*champs.values(), mid))

    def enregistrer_contenu(self, mid: int, transcription: str, synthese: dict, sujet: str, corps: str) -> None:
        with self._cx() as cx:
            cx.execute("INSERT OR REPLACE INTO contenus(message_id,transcription,synthese,sujet,corps) VALUES (?,?,?,?,?)",
                       (mid, transcription, json.dumps(synthese, ensure_ascii=False), sujet, corps))

    def contenu(self, mid: int):
        with self._cx() as cx:
            return cx.execute("SELECT * FROM contenus WHERE message_id=?", (mid,)).fetchone()

    def message(self, mid: int):
        with self._cx() as cx:
            return cx.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()

    def en_attente(self, limite: int = 50):
        with self._cx() as cx:
            return cx.execute("SELECT * FROM messages WHERE statut='a_notifier' ORDER BY COALESCE(urgence,0) DESC, id LIMIT ?",
                              (limite,)).fetchall()

    def envois_depuis(self, depuis_iso: str, urgents: bool) -> int:
        with self._cx() as cx:
            return cx.execute("SELECT COUNT(*) FROM messages WHERE notifie_le>=? AND (COALESCE(urgence,0)=5)=?",
                              (depuis_iso, 1 if urgents else 0)).fetchone()[0]

    def liste(self, jours: int = 7):
        depuis = maintenant_iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=jours))
        with self._cx() as cx:
            return cx.execute("SELECT * FROM messages WHERE recu_le>=? ORDER BY id DESC", (depuis,)).fetchall()

    def compter_par_statut(self) -> dict:
        with self._cx() as cx:
            return {r[0]: r[1] for r in cx.execute("SELECT statut, COUNT(*) FROM messages GROUP BY statut")}

    # ------------------------------------------------------------------ tâches
    def creer_tache(self, mid: int, destinataire: str, echeance_iso: str, libelle: str, numero: str) -> int:
        with self._cx() as cx:
            return cx.execute("INSERT INTO taches(message_id,destinataire,echeance,libelle,numero_rappel) VALUES (?,?,?,?,?)",
                              (mid, destinataire, echeance_iso, libelle, numero)).lastrowid

    def taches(self, faites: bool = False):
        with self._cx() as cx:
            return cx.execute("SELECT * FROM taches WHERE statut=? ORDER BY echeance, id", ("faite" if faites else "a_faire",)).fetchall()

    def taches_du_message(self, mid: int):
        with self._cx() as cx:
            return cx.execute("SELECT * FROM taches WHERE message_id=? ORDER BY id", (mid,)).fetchall()

    def tache_faite(self, tid: int) -> bool:
        with self._cx() as cx:
            return cx.execute("UPDATE taches SET statut='faite' WHERE id=? AND statut='a_faire'", (tid,)).rowcount > 0

    # ------------------------------------------------------------------ méta et purge
    def meta(self, cle: str, defaut: str = "") -> str:
        with self._cx() as cx:
            r = cx.execute("SELECT valeur FROM meta WHERE cle=?", (cle,)).fetchone()
            return r[0] if r else defaut

    def definir_meta(self, cle: str, valeur: str) -> None:
        with self._cx() as cx:
            cx.execute("INSERT OR REPLACE INTO meta(cle,valeur) VALUES (?,?)", (cle, valeur))

    def purger(self, conservation_jours: int, conservation_journal_jours: int, maintenant: dt.datetime | None = None) -> dict:
        """Supprime transcriptions, synthèses et tâches après `conservation_jours` ; le journal (sans texte) plus tard."""
        n = maintenant or dt.datetime.now(dt.timezone.utc)
        limite = maintenant_iso(n - dt.timedelta(days=conservation_jours))
        limite_j = maintenant_iso(n - dt.timedelta(days=conservation_journal_jours))
        with self._cx() as cx:
            c = cx.execute("DELETE FROM contenus WHERE message_id IN (SELECT id FROM messages WHERE recu_le<?)", (limite,)).rowcount
            t = cx.execute("DELETE FROM taches WHERE message_id IN (SELECT id FROM messages WHERE recu_le<?)", (limite,)).rowcount
            # un message encore à notifier ne peut plus l'être sans son contenu : on le signale au lieu de le laisser traîner
            cx.execute("UPDATE messages SET statut='expire', detail='contenu purgé avant l''envoi' "
                       "WHERE statut='a_notifier' AND id NOT IN (SELECT message_id FROM contenus)")
            m = cx.execute("DELETE FROM messages WHERE recu_le<?", (limite_j,)).rowcount
        return {"contenus": c, "taches": t, "journal": m}
