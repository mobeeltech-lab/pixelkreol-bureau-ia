"""Boîte mail dédiée (IMAP, lecture) et envoi des synthèses (SMTP).

Côté serveur de messagerie, le standard ne fait que : lire (BODY.PEEK, qui ne change aucun drapeau), puis marquer « lu » ou
copier dans le dossier « Traité ». Il ne supprime jamais un message (le seul retrait de la boîte de réception se fait APRÈS une copie réussie).
"""
from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import imaplib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from . import audio
from .config import ADRESSE_RE, Config
from .classement import chiffres, normaliser_numero, numeros_dans

LOG = logging.getLogger("abi_standard.messagerie")


@dataclass
class PieceJointe:
    nom: str
    type_mime: str
    octets: bytes
    audio: bool = False


@dataclass
class Courrier:
    cle: str
    expediteur: str
    sujet: str
    date: str
    texte: str
    pieces: list[PieceJointe] = field(default_factory=list)
    numero_entete: str = ""              # numéro de l'appelant trouvé dans l'objet, le nom du fichier ou le corps
    trop_gros: bool = False
    illisible: bool = False
    uid: str = ""


def nom_sur(nom: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", nom or "sans-nom")[:80]


def lire_courrier(brut: bytes, uid: str = "") -> Courrier:
    """Analyse un e-mail. Ne plante jamais : un mail illisible devient un Courrier marqué `illisible`."""
    cle = hashlib.sha256(brut).hexdigest()[:40]
    try:
        m = email.message_from_bytes(brut, policy=email.policy.default)
        mid = (m.get("Message-ID") or "").strip()
        if mid:
            cle = hashlib.sha256(mid.encode("utf-8", "replace")).hexdigest()[:40]
        sujet = re.sub(r"[\r\n\x00-\x1f]+", " ", str(m.get("Subject") or ""))[:200]
        expediteur = parseaddr(str(m.get("From") or ""))[1].lower()
        corps = m.get_body(preferencelist=("plain", "html"))
        texte = ""
        if corps is not None:
            texte = corps.get_content()
            if corps.get_content_type() == "text/html":
                texte = re.sub(r"<[^>]+>", " ", texte)
        texte = re.sub(r"[ \t]+", " ", texte)[:5000]
        pieces = []
        for p in m.iter_attachments():
            charge = p.get_payload(decode=True) or b""
            nom = p.get_filename() or ""
            tm = p.get_content_type()
            pieces.append(PieceJointe(nom_sur(nom), tm, charge, audio.annonce_audio(tm, nom) and not audio.semble_non_audio(charge[:16])))
        # une pièce « audio » est parfois incluse comme corps (multipart/alternative d'un seul tenant)
        if not pieces and m.get_content_maintype() == "audio":
            charge = m.get_payload(decode=True) or b""
            pieces.append(PieceJointe(nom_sur(m.get_filename() or "message.wav"), m.get_content_type(), charge,
                                      not audio.semble_non_audio(charge[:16])))
        numero = ""
        for source in [sujet, *[p.nom for p in pieces], texte[:1500], str(m.get("From") or "")]:
            trouves = numeros_dans(source)
            if trouves:
                numero = trouves[0]
                break
        return Courrier(cle, expediteur, sujet, str(m.get("Date") or ""), texte.strip(), pieces, numero, uid=uid)
    except Exception as e:  # noqa: BLE001
        LOG.warning("e-mail illisible (%s)", type(e).__name__)
        return Courrier(cle, "", "", "", "", [], "", illisible=True, uid=uid)


# ---------------------------------------------------------------------- IMAP
def utf7_imap(texte: str) -> str:
    """« Traité » → « Trait&AOk- » : imaplib n'accepte que l'ASCII, les accents passent par l'UTF-7 modifié (RFC 3501)."""
    sortie, tampon = [], []

    def vider():
        if tampon:
            b = "".join(tampon).encode("utf-16-be")
            sortie.append("&" + base64.b64encode(b).decode().rstrip("=").replace("/", ",") + "-")
            tampon.clear()
    for c in texte:
        if 0x20 <= ord(c) <= 0x7E:
            vider()
            sortie.append("&-" if c == "&" else c)
        else:
            tampon.append(c)
    vider()
    return "".join(sortie)


class Releve:
    """Une session IMAP. `fabrique` permet de brancher un faux serveur dans les tests."""

    def __init__(self, cfg: Config, fabrique=None):
        self.cfg = cfg
        if fabrique is None:
            if cfg.imap_ssl:
                fabrique = lambda: imaplib.IMAP4_SSL(cfg.imap_hote, cfg.imap_port, ssl_context=ssl.create_default_context(), timeout=30)
            else:
                fabrique = lambda: imaplib.IMAP4(cfg.imap_hote, cfg.imap_port, timeout=30)
        self._fabrique = fabrique
        self.cx = None

    def __enter__(self):
        self.cx = self._fabrique()
        self.cx.login(self.cfg.imap_compte, self.cfg.imap_mdp)
        typ, _ = self.cx.select(self._nom(self.cfg.dossier_source))
        if typ != "OK":
            raise imaplib.IMAP4.error(f"dossier {self.cfg.dossier_source} introuvable")
        return self

    def __exit__(self, *a):
        try:
            self.cx.logout()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _nom(dossier: str) -> str:
        """Nom de dossier au format IMAP (UTF-7 modifié pour les accents), entre guillemets."""
        return '"' + utf7_imap(dossier).replace("\\", "\\\\").replace('"', '\\"') + '"'

    def non_lus(self) -> list[str]:
        typ, data = self.cx.uid("SEARCH", None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return []
        return [u.decode() for u in data[0].split()]

    def lire(self, uid: str) -> Courrier:
        taille_max = self.cfg.taille_max_mo * 1024 * 1024 * 1.5 + 200_000     # une pièce jointe est encodée en base64 : ~ +37 %
        typ, data = self.cx.uid("FETCH", uid, "(RFC822.SIZE)")
        m = re.search(rb"RFC822\.SIZE (\d+)", b" ".join(x if isinstance(x, bytes) else x[0] for x in data if x) if data else b"")
        if typ == "OK" and m and int(m.group(1)) > taille_max:
            return Courrier(f"gros-{self.cfg.imap_compte}-{uid}-{m.group(1).decode()}", "", "", "", "", [], "", trop_gros=True, uid=uid)
        typ, data = self.cx.uid("FETCH", uid, "(BODY.PEEK[])")
        brut = next((x[1] for x in data or [] if isinstance(x, tuple)), b"")
        if typ != "OK" or not brut:
            return Courrier(f"vide-{self.cfg.imap_compte}-{uid}", "", "", "", "", [], "", illisible=True, uid=uid)
        return lire_courrier(brut, uid)

    def marquer_traite(self, uid: str) -> None:
        """Marque « lu » ; en mode « ranger », copie d'abord dans le dossier traité et ne retire de la boîte qu'après une copie réussie."""
        self.cx.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        if self.cfg.apres != "ranger":
            return
        dossier = self._nom(self.cfg.dossier_traite)
        typ, _ = self.cx.uid("COPY", uid, dossier)
        if typ != "OK":
            self.cx.create(dossier)
            typ, _ = self.cx.uid("COPY", uid, dossier)
        if typ == "OK":
            self.cx.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            self.cx.expunge()
        else:
            LOG.warning("rangement impossible pour le message %s : il reste dans la boîte (marqué lu)", uid)


# ---------------------------------------------------------------------- SMTP
class RefusDestinataire(Exception):
    pass


class Envoyeur:
    """Seul point de sortie du standard. Les destinataires sont contrôlés ICI, quel que soit l'appelant."""

    def __init__(self, cfg: Config, fabrique=None):
        self.cfg = cfg
        self._fabrique = fabrique

    def autorises(self) -> set[str]:
        c = self.cfg
        return set(c.dest_defaut) | set(c.dest_urgence) | {a for l in c.destinataires.values() for a in l}

    def verifier(self, destinataires: list[str]) -> list[str]:
        ok = []
        interdits = self.autorises()
        for a in destinataires:
            a = (a or "").strip().lower()
            if not ADRESSE_RE.match(a) or a not in interdits:
                raise RefusDestinataire("destinataire hors de la configuration")
            if a == self.cfg.imap_compte.lower():
                raise RefusDestinataire("la boîte dédiée n'est pas un destinataire")
            if self.cfg.domaines_ok and a.rsplit("@", 1)[-1] not in self.cfg.domaines_ok:
                raise RefusDestinataire("destinataire hors des domaines autorisés")
            if a not in ok:
                ok.append(a)
        if not ok:
            raise RefusDestinataire("aucun destinataire")
        return ok

    def envoyer(self, destinataires: list[str], sujet: str, corps: str, urgent: bool = False) -> list[str]:
        a = self.verifier(destinataires)
        m = EmailMessage()
        m["From"] = self.cfg.smtp_expediteur
        m["To"] = ", ".join(a)
        m["Subject"] = re.sub(r"[\r\n]+", " ", sujet)[:200]
        m["Date"] = formatdate(localtime=True)
        m["Message-ID"] = make_msgid(domain=(self.cfg.smtp_expediteur.rsplit("@", 1)[-1] or "localhost"))
        m["Auto-Submitted"] = "auto-generated"                     # évite les réponses automatiques en boucle
        m["X-Auto-Response-Suppress"] = "All"
        if urgent:
            m["X-Priority"], m["Importance"] = "1", "high"
        m.set_content(corps)
        c = self.cfg
        if self._fabrique:
            s = self._fabrique()
        elif c.smtp_securite == "ssl":
            s = smtplib.SMTP_SSL(c.smtp_hote, c.smtp_port, timeout=30, context=ssl.create_default_context())
        else:
            s = smtplib.SMTP(c.smtp_hote, c.smtp_port, timeout=30)
        try:
            if c.smtp_securite == "starttls" and not self._fabrique:
                s.starttls(context=ssl.create_default_context())
            if c.smtp_compte:
                s.login(c.smtp_compte, c.smtp_mdp)
            s.send_message(m, from_addr=c.smtp_expediteur, to_addrs=a)
        finally:
            try:
                s.quit()
            except Exception:  # noqa: BLE001
                pass
        return a
