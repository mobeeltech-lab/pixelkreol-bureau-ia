"""Banc d'essai du standard téléphonique : messages vocaux FICTIFS, faux serveurs IMAP et SMTP (vrai protocole, sur 127.0.0.1),
fausse Secrétaire et faux moteur de transcription. Aucune vraie voix, aucun vrai numéro, aucune vraie adresse.

Numéros : plages réservées à la fiction par l'Arcep (06 39 98 xx xx, 04 65 71 12 xx). Domaines : .example (réservé, ne résout jamais).
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import re
import socketserver
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

import httpx

# =====================================================================  les messages fictifs
@dataclass
class MessageFictif:
    cle: str
    dit: str                      # ce que dit la voix de synthèse = transcription attendue
    categorie: str
    urgence: int
    dest: str                     # destinataire attendu
    numero_dit: str | None = None
    numero_entete: str = "0639981201"
    nom: str | None = None
    ia_urgence: int | None = None  # ce que la fausse Secrétaire répond (peut minimiser : l'urgence 5 doit tenir quand même)


MESSAGES = [
    MessageFictif("vente", "Bonjour, ici Pierre Test de la société Exemple. Je souhaite visiter un appartement trois pièces du programme "
                  "Les Oliviers. Rappelez-moi au 06 39 98 12 34. Merci.", "vente", 2, "commercial@abi.example", "0639981234", "0639981201", "Pierre Test"),
    MessageFictif("location", "Bonjour, c'est Madame Fictive, locataire. Ma quittance de loyer d'octobre n'est pas arrivée. "
                  "Vous pouvez me rappeler au 04 65 71 12 05. Merci.", "location", 2, "locatif@abi.example", "0465711205", "0639981202", "Madame Fictive"),
    MessageFictif("fonds", "Bonjour, je suis le notaire de monsieur Exemple, au sujet de l'appel de fonds de la tranche hors d'eau. "
                  "Merci de me rappeler.", "appel_de_fonds", 3, "fonds@abi.example", None, "0639981203", "Maître Exemple"),
    MessageFictif("accident", "Allô, accident sur le chantier des Oliviers, un ouvrier est blessé, les pompiers arrivent. "
                  "Rappelez-moi vite au 06 39 98 00 11.", "chantier", 5, "chantier@abi.example", "0639980011", "0639981204", None, ia_urgence=2),
    MessageFictif("fournisseur", "Bonjour, entreprise Béton Fictif. Je vous appelle au sujet de la facture quatre cent douze et du devis "
                  "pour la dalle. Rappelez-moi au 06 39 98 45 67.", "fournisseur", 3, "compta@abi.example", "0639984567", "0639981205", "Béton Fictif"),
    MessageFictif("mairie", "Bonjour, service urbanisme de la mairie. Votre permis de construire est prêt, passez le retirer cette semaine.",
                  "administration", 3, "juridique@abi.example", None, "0465711299", "Mairie fictive"),
    MessageFictif("degats", "Bonjour, j'ai un dégât des eaux dans mon appartement, l'eau coule chez le voisin du dessous. "
                  "C'est le 06 39 98 77 88. Faites vite.", "location", 5, "locatif@abi.example", "0639987788", "0639981207", None, ia_urgence=3),
    MessageFictif("autre", "Bonjour, je voulais juste savoir si vos bureaux sont ouverts le mercredi après-midi. Merci, au revoir.",
                  "autre", 1, "secretariat@abi.example", None, "0639981208", None),
]
PAR_CLE = {m.cle: m for m in MESSAGES}

# adresses (toutes fictives)
DEST = {"vente": "commercial@abi.example", "location": "locatif@abi.example", "appel_de_fonds": "fonds@abi.example",
        "chantier": "chantier@abi.example", "fournisseur": "compta@abi.example", "administration": "juridique@abi.example",
        "autre": "secretariat@abi.example"}
DIRECTION, ASTREINTE, BOITE = "direction@abi.example", "astreinte@abi.example", "standard@abi.example"
OPERATEUR = "messagerie-vocale@operateur.example"


def env_standard(imap_port: int = 0, smtp_port: int = 0, dossier: Path | None = None, **plus) -> dict:
    e = {"ABI_STANDARD_ACTIF": "1", "ABI_STANDARD_IMAP_HOTE": "127.0.0.1", "ABI_STANDARD_IMAP_PORT": str(imap_port),
         "ABI_STANDARD_IMAP_SSL": "0", "ABI_STANDARD_IMAP_COMPTE": BOITE, "ABI_STANDARD_IMAP_MDP": "mdp-de-test",
         "ABI_STANDARD_SMTP_HOTE": "127.0.0.1", "ABI_STANDARD_SMTP_PORT": str(smtp_port), "ABI_STANDARD_SMTP_SECURITE": "aucune",
         "ABI_STANDARD_SMTP_EXPEDITEUR": "standard-ia@abi.example", "ABI_STANDARD_ORCH_CLE": "cle-orchestrateur-de-test",
         "ABI_STANDARD_DEST_DEFAUT": DIRECTION, "ABI_STANDARD_DEST_URGENCE": ASTREINTE, "ABI_STANDARD_DOMAINES_OK": "abi.example",
         "ABI_STANDARD_EXPEDITEURS_OK": OPERATEUR, "ABI_STANDARD_HORAIRES": "08:00-18:00", "ABI_STANDARD_JOURS": "1-5",
         "ABI_STANDARD_LOT_MAX": "20"}
    for cat, a in DEST.items():
        e["ABI_STANDARD_DEST_" + cat.upper()] = a
    if dossier is not None:
        e["ABI_STANDARD_DONNEES"] = str(dossier / "donnees")
        e["ABI_STANDARD_TMP"] = str(dossier / "tmp")
        (dossier / "tmp").mkdir(parents=True, exist_ok=True)
    e.update({k: str(v) for k, v in plus.items()})
    return e


# =====================================================================  fabrication des e-mails
def fabriquer_mail(audio: Path | bytes | None, numero: str = "0639981201", nom_fichier: str | None = None, type_mime: tuple[str, str] = ("audio", "wav"),
                   expediteur: str = OPERATEUR, sujet: str | None = None, corps: str = "Vous avez un nouveau message vocal.",
                   message_id: str | None = None, reply_to: str | None = None, autres: list[tuple[str, tuple[str, str], bytes]] | None = None) -> bytes:
    m = EmailMessage()
    m["From"] = f"Messagerie vocale <{expediteur}>"
    m["To"] = BOITE
    m["Subject"] = sujet if sujet is not None else f"Nouveau message vocal de {numero[:2]} {numero[2:4]} {numero[4:6]} {numero[6:8]} {numero[8:]}"
    m["Date"] = email.utils.formatdate(1_791_000_000)
    m["Message-ID"] = message_id or f"<{hashlib.sha1(repr((numero, sujet, corps, nom_fichier)).encode()).hexdigest()[:16]}@operateur.example>"
    if reply_to:
        m["Reply-To"] = reply_to
    m.set_content(corps)
    if audio is not None:
        octets = audio if isinstance(audio, bytes) else Path(audio).read_bytes()
        m.add_attachment(octets, maintype=type_mime[0], subtype=type_mime[1], filename=nom_fichier or f"msg_{numero}.wav")
    for nom, (mt, st), data in autres or []:
        m.add_attachment(data, maintype=mt, subtype=st, filename=nom)
    return m.as_bytes()


# =====================================================================  faux IMAP (vrai protocole, minimal)
class BoiteIMAP:
    def __init__(self):
        self.dossiers: dict[str, list[dict]] = {"INBOX": []}
        self.prochain_uid = 1
        self.compte, self.mdp = BOITE, "mdp-de-test"
        self.journal: list[str] = []          # commandes reçues (pour vérifier qu'aucune suppression n'est faite sans copie)
        self.panne_login = False
        self.verrou = threading.Lock()

    def deposer(self, brut: bytes, dossier: str = "INBOX", vu: bool = False) -> int:
        with self.verrou:
            uid = self.prochain_uid
            self.prochain_uid += 1
            self.dossiers.setdefault(dossier, []).append({"uid": uid, "brut": brut, "flags": {"\\Seen"} if vu else set()})
            return uid

    def non_lus(self, dossier: str = "INBOX") -> list[int]:
        return [m["uid"] for m in self.dossiers.get(dossier, []) if "\\Seen" not in m["flags"]]


class _IMAP(socketserver.StreamRequestHandler):
    def ecrire(self, texte: str | bytes):
        self.wfile.write(texte if isinstance(texte, bytes) else texte.encode("utf-8"))
        self.wfile.flush()

    def handle(self):
        boite: BoiteIMAP = self.server.boite
        dossier = None
        self.ecrire("* OK IMAP4rev1 faux serveur de test\r\n")
        while True:
            ligne = self.rfile.readline()
            if not ligne:
                return
            texte = ligne.decode("utf-8", "replace").rstrip("\r\n")
            if not texte:
                continue
            tag, _, reste = texte.partition(" ")
            cmd, _, args = reste.partition(" ")
            cmd = cmd.upper()
            boite.journal.append(f"{cmd} {args}".strip())
            if cmd == "CAPABILITY":
                self.ecrire(f"* CAPABILITY IMAP4rev1 UIDPLUS\r\n{tag} OK fait\r\n")
            elif cmd == "LOGIN":
                ok = not boite.panne_login and args.split(" ")[0].strip('"') == boite.compte and args.split(" ", 1)[1].strip('"') == boite.mdp
                self.ecrire(f"{tag} OK connecté\r\n" if ok else f"{tag} NO identifiants refusés\r\n")
            elif cmd == "SELECT":
                nom = args.strip().strip('"')
                if nom in boite.dossiers:
                    dossier = nom
                    n = len(boite.dossiers[nom])
                    self.ecrire(f"* {n} EXISTS\r\n* FLAGS (\\Seen \\Deleted)\r\n{tag} OK [READ-WRITE] fait\r\n")
                else:
                    self.ecrire(f"{tag} NO dossier inconnu\r\n")
            elif cmd == "CREATE":
                boite.dossiers.setdefault(args.strip().strip('"'), [])
                self.ecrire(f"{tag} OK créé\r\n")
            elif cmd == "UID":
                sous, _, a2 = args.partition(" ")
                sous = sous.upper()
                msgs = boite.dossiers.get(dossier or "", [])
                if sous == "SEARCH":
                    uids = " ".join(str(m["uid"]) for m in msgs if "\\Seen" not in m["flags"])
                    self.ecrire(f"* SEARCH {uids}\r\n{tag} OK fait\r\n".replace("SEARCH \r", "SEARCH\r"))
                elif sous == "FETCH":
                    uid_s, _, champs = a2.partition(" ")
                    m = next((x for x in msgs if str(x["uid"]) == uid_s), None)
                    if m is None:
                        self.ecrire(f"{tag} OK rien\r\n")
                    elif "RFC822.SIZE" in champs:
                        self.ecrire(f"* 1 FETCH (UID {uid_s} RFC822.SIZE {len(m['brut'])})\r\n{tag} OK fait\r\n")
                    else:
                        self.ecrire(f"* 1 FETCH (UID {uid_s} BODY[] {{{len(m['brut'])}}}\r\n".encode() + m["brut"] + b")\r\n" + f"{tag} OK fait\r\n".encode())
                elif sous == "STORE":
                    uid_s, _, reste2 = a2.partition(" ")
                    for m in msgs:
                        if str(m["uid"]) == uid_s:
                            for f in re.findall(r"\\\w+", reste2):
                                m["flags"].add(f)
                    self.ecrire(f"{tag} OK fait\r\n")
                elif sous == "COPY":
                    uid_s, _, cible = a2.partition(" ")
                    cible = cible.strip().strip('"')
                    if cible not in boite.dossiers:
                        self.ecrire(f"{tag} NO [TRYCREATE] dossier absent\r\n")
                    else:
                        m = next((x for x in msgs if str(x["uid"]) == uid_s), None)
                        if m:
                            boite.dossiers[cible].append({"uid": boite.prochain_uid, "brut": m["brut"], "flags": set(m["flags"])})
                            boite.prochain_uid += 1
                        self.ecrire(f"{tag} OK copié\r\n")
                else:
                    self.ecrire(f"{tag} BAD commande inconnue\r\n")
            elif cmd == "EXPUNGE":
                if dossier:
                    boite.dossiers[dossier] = [m for m in boite.dossiers[dossier] if "\\Deleted" not in m["flags"]]
                self.ecrire(f"{tag} OK fait\r\n")
            elif cmd == "NOOP":
                self.ecrire(f"{tag} OK\r\n")
            elif cmd == "LOGOUT":
                self.ecrire(f"* BYE au revoir\r\n{tag} OK fini\r\n")
                return
            else:
                self.ecrire(f"{tag} BAD non géré\r\n")


class ServeurIMAP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, boite: BoiteIMAP):
        super().__init__(("127.0.0.1", 0), _IMAP)
        self.boite = boite
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def arreter(self):
        self.shutdown()
        self.server_close()


# =====================================================================  faux SMTP
class BoiteSMTP:
    def __init__(self):
        self.recus: list[dict] = []      # {"de":…, "a":[…], "data": str}
        self.refuser = False             # simule un serveur d'envoi en panne


class _SMTP(socketserver.StreamRequestHandler):
    def ecrire(self, t):
        self.wfile.write((t + "\r\n").encode())
        self.wfile.flush()

    def handle(self):
        boite: BoiteSMTP = self.server.boite
        self.ecrire("220 faux serveur SMTP de test")
        de, a = "", []
        while True:
            ligne = self.rfile.readline()
            if not ligne:
                return
            t = ligne.decode("utf-8", "replace").rstrip("\r\n")
            u = t.upper()
            if u.startswith(("EHLO", "HELO")):
                self.ecrire("250 bonjour")
            elif u.startswith("MAIL FROM"):
                de, a = re.findall(r"<([^>]*)>", t)[0], []
                self.ecrire("250 ok")
            elif u.startswith("RCPT TO"):
                if boite.refuser:
                    self.ecrire("451 serveur indisponible")
                else:
                    a.append(re.findall(r"<([^>]*)>", t)[0])
                    self.ecrire("250 ok")
            elif u == "DATA":
                self.ecrire("354 envoyez")
                morceaux = []
                while True:
                    l = self.rfile.readline()
                    if l in (b".\r\n", b""):
                        break
                    morceaux.append(l[1:] if l.startswith(b"..") else l)
                boite.recus.append({"de": de, "a": list(a), "data": b"".join(morceaux).decode("utf-8", "replace")})
                self.ecrire("250 reçu")
            elif u == "QUIT":
                self.ecrire("221 au revoir")
                return
            else:
                self.ecrire("250 ok")


class ServeurSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, boite: BoiteSMTP):
        super().__init__(("127.0.0.1", 0), _SMTP)
        self.boite = boite
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def arreter(self):
        self.shutdown()
        self.server_close()


def message_recu(brut: dict) -> EmailMessage:
    import email as _email
    import email.policy
    return _email.message_from_string(brut["data"], policy=email.policy.default)


def corps_recu(brut: dict) -> str:
    return message_recu(brut).get_body(preferencelist=("plain",)).get_content()


# =====================================================================  fausse Secrétaire (Bureau IA)
class FausseSecretaire:
    """Répond comme le ferait la Secrétaire. `minimise` : elle sous-estime volontairement l'urgence (pour prouver le plancher de sécurité)."""

    def __init__(self):
        self.appels: list[dict] = []
        self.panne: int | None = None          # code HTTP à renvoyer, ou None
        self.reponse_brute: str | None = None  # texte à renvoyer tel quel (réponse illisible, injection…)
        self.exception: Exception | None = None

    def __call__(self, requete: httpx.Request) -> httpx.Response:
        corps = json.loads(requete.content)
        self.appels.append({"entetes": dict(requete.headers), "corps": corps, "url": str(requete.url)})
        if self.exception:
            raise self.exception
        if self.panne:
            return httpx.Response(self.panne, json={"error": {"message": "indisponible"}})
        texte = corps["messages"][-1]["content"]
        dit = re.search(r"<message>\n(.*?)\n</message>", texte, re.S).group(1)
        contenu = self.reponse_brute if self.reponse_brute is not None else json.dumps(self.analyser(dit), ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": contenu}}]})

    @staticmethod
    def analyser(dit: str) -> dict:
        m = next((x for x in MESSAGES if x.dit[:40] == dit[:40]), None)
        if m is None:
            return {"appelant": {"nom": None, "societe": None, "numero_rappel": None}, "motif": "message divers", "categorie": "autre",
                    "urgence": 2, "resume": ["Message divers.", "À lire."], "action_proposee": "Lire et rappeler.", "agent": "secretaire"}
        return {"appelant": {"nom": m.nom, "societe": None, "numero_rappel": m.numero_dit},
                "motif": f"Appel ({m.categorie.replace('_', ' ')})", "categorie": m.categorie,
                "urgence": m.ia_urgence if m.ia_urgence is not None else m.urgence,
                "resume": [f"Résumé ligne 1 ({m.cle}).", "Résumé ligne 2."], "action_proposee": "Rappeler la personne.",
                "agent": {"vente": "commercial", "location": "locatif", "appel_de_fonds": "appels-de-fonds", "chantier": "suivi-programme",
                          "fournisseur": "comptable", "administration": "juriste", "autre": "secretaire"}[m.categorie]}

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


# =====================================================================  faux moteur de transcription
class FauxTranscripteur:
    """Même contrat que FasterWhisper. Sert la transcription enregistrée pour l'empreinte du fichier audio (celle de la voix de synthèse)."""

    def __init__(self):
        self.table: dict[str, str] = {}
        self.appels: list[tuple[str, int]] = []
        self.liberes = 0
        self.panne: Exception | None = None
        self.en_cours = 0
        self.simultanes_max = 0
        self.fichiers_vus: list[Path] = []

    def enregistrer(self, chemin: Path, texte: str) -> None:
        self.table[hashlib.sha256(Path(chemin).read_bytes()).hexdigest()] = texte

    def transcrire(self, chemin: Path, duree_max_s: int):
        from abi_standard import audio
        from abi_standard.transcription import Transcription
        self.en_cours += 1
        self.simultanes_max = max(self.simultanes_max, self.en_cours)
        try:
            self.fichiers_vus.append(Path(chemin))
            assert Path(chemin).exists(), "l'audio doit exister PENDANT la transcription"
            self.appels.append((Path(chemin).name, duree_max_s))
            if self.panne:
                raise self.panne
            duree = audio.duree(Path(chemin)) or 0.0
            octets = Path(chemin).read_bytes()
            texte = self.table.get(hashlib.sha256(octets).hexdigest())
            if texte is None:
                raise audio.ErreurAudio("fichier audio illisible (inconnu du faux moteur)")
            return Transcription(texte, duree, duree > duree_max_s, 0.9, 0.01)
        finally:
            self.en_cours -= 1

    def liberer(self) -> None:
        self.liberes += 1
