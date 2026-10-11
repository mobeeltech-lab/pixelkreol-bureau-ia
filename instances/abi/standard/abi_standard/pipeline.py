"""Le standard : relève → audio → transcription locale → Secrétaire → synthèse par e-mail + tâche → journal.

Règles qui ne bougent jamais :
  * un message à la fois (file d'attente d'un), `lot_max` messages au plus par passage ;
  * l'audio est écrit dans un fichier temporaire privé et SUPPRIMÉ dès la transcription, succès ou échec ;
  * aucun e-mail ne part vers un appelant, aucun rappel n'est lancé : le standard prépare, une personne agit ;
  * aucune panne (audio cassé, Secrétaire absente, SMTP en erreur…) n'arrête la boucle ni ne fait perdre un message.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import logging
import os
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from . import audio, classement
from .config import AGENT_PAR_CATEGORIE, Config
from .horaires import Horaires
from .journal import Journal, maintenant_iso
from .messagerie import Courrier, Envoyeur, PieceJointe, RefusDestinataire, Releve, nom_sur
from .classement import Synthese, formater_numero, masquer_numero, normaliser_numero, propre
from .transcription import Transcripteur, compter_mots, est_hallucination

LOG = logging.getLogger("abi_standard")

CATEGORIES_LISIBLES = {"vente": "Vente", "location": "Location", "appel_de_fonds": "Appel de fonds", "chantier": "Chantier",
                       "fournisseur": "Fournisseur", "administration": "Administration", "autre": "Autre"}
AGENTS_LISIBLES = {"orchestrateur": "Directeur de cabinet", "secretaire": "Secrétaire", "comptable": "Comptable",
                   "appels-de-fonds": "Appels de fonds", "commercial": "Commercial", "locatif": "Gestionnaire locatif",
                   "juriste": "Juriste", "suivi-programme": "Suivi de programme", "archiviste": "Archiviste"}
RAISONS = {"sans_audio": "mail sans message vocal", "audio_illisible": "fichier audio illisible", "inaudible": "message vide ou inaudible",
           "trop_gros": "fichier trop volumineux", "mail_illisible": "e-mail illisible", "piece_non_audio": "pièce jointe qui n'est pas de l'audio"}
MAX_AUDIOS_PAR_MAIL = 3
MAX_ENVOIS_ESSAIS = 20


class PanneMoteur(Exception):
    """La transcription est en panne (modèle absent, mémoire…) : le message est LAISSÉ non lu et sera repris."""


def jour_fr(t: dt.datetime) -> str:
    return t.strftime("%d/%m/%Y à %H:%M")


class Standard:
    def __init__(self, cfg: Config, transcripteur: Transcripteur, journal: Journal | None = None, releve_fabrique=None,
                 smtp_fabrique=None, client_http=None, horloge=None):
        self.cfg = cfg
        self.transcripteur = transcripteur
        self.journal = journal or Journal(cfg.base)
        self.releve_fabrique = releve_fabrique
        self.envoyeur = Envoyeur(cfg, smtp_fabrique)
        self.client_http = client_http
        self.horloge = horloge or (lambda: dt.datetime.now(dt.timezone.utc))
        self.tz = ZoneInfo(cfg.fuseau)
        self.horaires = Horaires(cfg.horaires, cfg.jours)
        self.echecs_releve = 0
        self.echecs_moteur = 0

    # ------------------------------------------------------------------ utilitaires
    def maintenant(self) -> dt.datetime:
        return self.horloge()

    def local(self) -> dt.datetime:
        return self.maintenant().astimezone(self.tz)

    def nettoyer_tmp(self) -> int:
        """Au démarrage : supprime les restes d'un arrêt brutal (un audio ne doit jamais traîner)."""
        n = 0
        for f in Path(self.cfg.dossier_tmp).glob("abi-standard-*"):
            with contextlib.suppress(OSError):
                f.unlink()
                n += 1
        return n

    def destinataires_pour(self, categorie: str, urgence: int) -> list[str]:
        c = self.cfg
        liste = list(c.destinataires.get(categorie, []))
        if urgence == 5:
            liste += c.dest_urgence
        if not liste:
            liste = list(c.dest_defaut)
        sortie = []
        for a in liste:
            if a not in sortie:
                sortie.append(a)
        return sortie

    # ------------------------------------------------------------------ un passage
    def cycle(self) -> dict:
        stats = {"traites": 0, "notifies": 0, "erreurs": 0, "occupe": False, "panne": ""}
        with self._verrou() as libre:
            if not libre:
                stats["occupe"] = True
                return stats
            self._purge_si_besoin()
            stats["notifies"] += self.reprendre_notifications()
            try:
                with Releve(self.cfg, self.releve_fabrique) as r:
                    for uid in r.non_lus()[: self.cfg.lot_max]:
                        courrier = r.lire(uid)
                        try:
                            self.traiter(courrier)
                        except PanneMoteur as e:
                            stats["panne"] = str(e)
                            break
                        except Exception:  # noqa: BLE001 — un message ne doit jamais arrêter la boucle
                            LOG.exception("échec inattendu sur un message")
                            stats["erreurs"] += 1
                            self._noter_erreur(courrier)
                        r.marquer_traite(uid)
                        stats["traites"] += 1
                self.echecs_releve = 0
            except Exception as e:  # noqa: BLE001 — réseau, identifiants… : on réessaiera au prochain passage
                self.echecs_releve += 1
                stats["panne"] = f"relève impossible ({type(e).__name__})"
                LOG.warning("relève impossible (%s), échec n°%s", type(e).__name__, self.echecs_releve)
                if self.echecs_releve == 5:
                    self.alerter_admin("releve", "La relève de la boîte vocale échoue depuis 5 passages",
                                       "Le standard n'arrive plus à lire la boîte dédiée (identifiants, réseau ou serveur de messagerie). "
                                       "Les messages vocaux ne sont PAS perdus : ils restent dans la boîte et seront traités au retour de la connexion.")
            if stats["panne"].startswith("transcription"):
                self.echecs_moteur += 1
                if self.echecs_moteur == 3:
                    self.alerter_admin("moteur", "La transcription des messages vocaux est en panne",
                                       "Le moteur de transcription ne démarre pas (modèle absent ? mémoire ?). Les messages restent non lus "
                                       "dans la boîte et seront repris dès que le moteur fonctionne. Voir STANDARD-LISEZMOI.md, « Dépannage ».")
            else:
                self.echecs_moteur = 0
            if self.cfg.liberer_modele:
                self.transcripteur.liberer()
        return stats

    @contextlib.contextmanager
    def _verrou(self):
        """Un seul passage à la fois, même si deux commandes sont lancées ensemble."""
        chemin = Path(self.cfg.dossier_donnees)
        chemin.mkdir(parents=True, exist_ok=True)
        f = open(chemin / "standard.lock", "w")
        try:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            yield True
        finally:
            f.close()

    def _purge_si_besoin(self) -> None:
        derniere = self.journal.meta("derniere_purge", "")
        if derniere[:13] != maintenant_iso(self.maintenant())[:13]:           # au plus une fois par heure
            r = self.journal.purger(self.cfg.conservation_jours, self.cfg.conservation_journal_jours, self.maintenant())
            self.journal.definir_meta("derniere_purge", maintenant_iso(self.maintenant()))
            if any(r.values()):
                LOG.info("purge : %s", r)

    # ------------------------------------------------------------------ un courrier
    def traiter(self, c: Courrier) -> None:
        recu = maintenant_iso(self.maintenant())
        masque = masquer_numero(c.numero_entete) if c.numero_entete else "inconnu"
        if c.illisible or c.trop_gros:
            self._notice(c.cle, recu, masque, "mail_illisible" if c.illisible else "trop_gros", c, "")
            return
        if self.cfg.expediteurs_ok and c.expediteur not in self.cfg.expediteurs_ok:
            mid = self.journal.ouvrir(c.cle, recu, masque)
            if mid:
                self.journal.mettre_a_jour(mid, statut="ignore", traite_le=recu, detail="expéditeur non autorisé")
            return
        audios = [p for p in c.pieces if p.audio]
        if not audios:
            autres = ", ".join(f"{p.nom or 'sans nom'} ({p.type_mime})" for p in c.pieces[:5])
            statut = "piece_non_audio" if c.pieces else "sans_audio"
            self._notice(c.cle, recu, masque, statut, c, f"Pièces jointes ignorées (jamais ouvertes) : {autres}" if autres else "")
            return
        for i, p in enumerate(audios[:MAX_AUDIOS_PAR_MAIL]):
            if len(p.octets) > self.cfg.taille_max_mo * 1024 * 1024:
                self._notice(f"{c.cle}#{i}", recu, masque, "trop_gros", c, f"{p.nom} : {len(p.octets) // 1024 // 1024} Mo")
            else:
                self._traiter_audio(c, p, f"{c.cle}#{i}", recu, masque)

    def _traiter_audio(self, c: Courrier, p: PieceJointe, cle: str, recu: str, masque: str) -> None:
        mid = self.journal.ouvrir(cle, recu, masque)
        if mid is None:
            return                                             # déjà traité (le marquage « lu » avait échoué)
        chemin = None
        try:
            fd, nom = tempfile.mkstemp(prefix="abi-standard-", suffix=audio.extension(p.nom) or ".bin", dir=self.cfg.dossier_tmp)
            chemin = Path(nom)
            with os.fdopen(fd, "wb") as f:
                f.write(p.octets)
            p.octets = b""
            duree = audio.duree(chemin)
            if duree is None:
                raise audio.ErreurAudio("durée illisible")
            tr = self.transcripteur.transcrire(chemin, self.cfg.duree_max_s)
        except audio.ErreurAudio as e:
            LOG.warning("audio inexploitable (%s)", e)
            self._finir_notice(mid, "audio_illisible", c, str(e))
            return
        except Exception as e:  # noqa: BLE001 — moteur en panne : rien n'est perdu, le message reste non lu
            LOG.error("transcription impossible (%s)", type(e).__name__)
            self.journal.oublier(mid)
            raise PanneMoteur(f"transcription impossible ({type(e).__name__})") from e
        finally:
            if chemin is not None:
                with contextlib.suppress(OSError):
                    chemin.unlink()                           # l'audio disparaît du serveur, quoi qu'il arrive
        tronque = bool(tr.tronque or (tr.duree_s and tr.duree_s > self.cfg.duree_max_s + 1))
        self.journal.mettre_a_jour(mid, duree_audio=round(tr.duree_s or 0, 1), secondes_calcul=round(tr.secondes_calcul, 1), tronque=int(tronque))
        texte = tr.texte
        alerte = classement.urgence_5_detectee(texte)
        if not alerte and (compter_mots(texte) < self.cfg.mots_min or est_hallucination(texte)):
            self._finir_notice(mid, "inaudible", c, "", transcription="" if est_hallucination(texte) else texte)
            return
        s = classement.classer(texte, self.cfg, self.client_http)
        self._finir_synthese(mid, c, s, texte, tronque, tr.duree_s)

    # ------------------------------------------------------------------ résultats
    def _numero(self, c: Courrier, s: Synthese | None) -> tuple[str, str]:
        if s and s.numero_dit:
            return s.numero_dit, "dit dans le message"
        if c.numero_entete:
            return c.numero_entete, "trouvé dans l'en-tête du mail de l'opérateur (non confirmé par l'appelant)"
        return "", "inconnu"

    def _hors_horaires(self) -> bool:
        return not self.horaires.ouvert(self.local())

    def _finir_synthese(self, mid: int, c: Courrier, s: Synthese, texte: str, tronque: bool, duree_s) -> None:
        local = self.local()
        numero, source = self._numero(c, s)
        dest = self.destinataires_pour(s.categorie, s.urgence)
        echeance = local if s.urgence == 5 else self.horaires.echeance(local, classement.URGENCE_ECHEANCE_H[s.urgence])
        qui = " · ".join(x for x in (s.nom, s.societe) if x) or (masquer_numero(numero) if numero else "appelant inconnu")
        sujet = (f"[URGENT 5/5 — ALERTE] {CATEGORIES_LISIBLES[s.categorie]} · {qui}" if s.urgence == 5
                 else f"[Standard ABI] {CATEGORIES_LISIBLES[s.categorie]} · urgence {s.urgence}/5 · {qui}")
        tid = None
        if dest:
            tid = self.journal.creer_tache(mid, dest[0], maintenant_iso(echeance),
                                           f"Rappeler {qui} — {s.motif}"[:300], numero)
        corps = self._corps_synthese(s, texte, local, numero, source, qui, tronque, duree_s, dest, tid, echeance)
        self.journal.enregistrer_contenu(mid, texte, s.dictionnaire(), sujet, corps)
        self.journal.mettre_a_jour(mid, categorie=s.categorie, urgence=s.urgence, destinataires=",".join(dest), classement=s.classement,
                                   numero_masque=masquer_numero(numero) if numero else "inconnu",
                                   traite_le=maintenant_iso(self.maintenant()),
                                   statut="a_notifier" if dest else "sans_destinataire",
                                   detail="" if dest else "aucun destinataire configuré pour cette catégorie")
        if not dest:
            LOG.error("message %s sans destinataire (catégorie %s)", mid, s.categorie)
            return
        self._notifier(mid)

    def _notice(self, cle: str, recu: str, masque: str, statut: str, c: Courrier, detail: str) -> None:
        mid = self.journal.ouvrir(cle, recu, masque)
        if mid is not None:
            self._finir_notice(mid, statut, c, detail)

    def _finir_notice(self, mid: int, statut: str, c: Courrier, detail: str, transcription: str = "") -> None:
        """Cas particuliers (rien à classer) : on prévient quand même une personne, qui ira écouter l'original."""
        local = self.local()
        numero, source = self._numero(c, None)
        dest = list(self.cfg.dest_defaut)
        raison = RAISONS.get(statut, statut)
        echeance = self.horaires.echeance(local, 4)
        sujet = f"[Standard ABI] Message à écouter : {raison}"
        tid = self.journal.creer_tache(mid, dest[0], maintenant_iso(echeance), f"Écouter le message original ({raison})", numero) if dest else None
        lignes = [f"Un message est arrivé le {jour_fr(local)} mais le standard n'a pas pu le classer : {raison}.", ""]
        if numero:
            lignes.append(f"Numéro de l'appelant : {formater_numero(numero)} ({source}).")
        if c.sujet:
            lignes.append(f"Objet du mail de l'opérateur : {c.sujet}")
        if detail:
            lignes.append(detail)
        if transcription:
            lignes += ["", "Ce que le standard a cru entendre (très incomplet) :", f"> {transcription}"]
        if c.texte and statut in ("sans_audio", "piece_non_audio"):
            lignes += ["", "Texte du mail (début) :", "> " + " ".join(c.texte.split())[:500]]
        lignes += ["", "À FAIRE : écouter le message original dans la boîte vocale de l'opérateur (le mail y est resté).",
                   f"Tâche n°{tid} à faire avant le {jour_fr(echeance.astimezone(self.tz))}." if tid else "",
                   "", self.pied()]
        self.journal.enregistrer_contenu(mid, transcription, {"statut": statut}, sujet, "\n".join(x for x in lignes if x is not None))
        self.journal.mettre_a_jour(mid, categorie="autre", urgence=3, destinataires=",".join(dest), traite_le=maintenant_iso(self.maintenant()),
                                   statut="a_notifier" if dest else "sans_destinataire", detail=raison)
        if dest:
            self._notifier(mid)

    def _noter_erreur(self, c: Courrier) -> None:
        mid = self.journal.ouvrir(c.cle + "!err", maintenant_iso(self.maintenant()))
        if mid:
            self.journal.mettre_a_jour(mid, statut="erreur", detail="erreur inattendue (voir les journaux du conteneur)")
            self.alerter_admin("erreur-" + c.cle[:8], "Un message vocal n'a pas pu être traité",
                               "Une erreur inattendue a empêché le traitement d'un message. Il reste dans la boîte vocale de l'opérateur : "
                               "à écouter directement. Détail technique dans les journaux du conteneur (docker compose logs abi_standard).")

    # ------------------------------------------------------------------ envoi
    def _notifier(self, mid: int) -> bool:
        m = self.journal.message(mid)
        contenu = self.journal.contenu(mid)
        if m is None or contenu is None:
            return False
        urgent = m["urgence"] == 5
        depuis = maintenant_iso(self.maintenant() - dt.timedelta(hours=1))
        limite = self.cfg.max_alertes_heure if urgent else self.cfg.max_mails_heure
        if self.journal.envois_depuis(depuis, urgent) >= limite:
            LOG.warning("limite d'envois par heure atteinte (%s) : message %s gardé pour plus tard", limite, mid)
            return False
        dest = [a for a in (m["destinataires"] or "").split(",") if a]
        try:
            envoyes = self.envoyeur.envoyer(dest, contenu["sujet"], contenu["corps"], urgent=urgent)
        except RefusDestinataire as e:
            LOG.error("envoi refusé pour le message %s : %s", mid, e)
            self.journal.mettre_a_jour(mid, statut="refuse", detail=str(e))
            return False
        except Exception as e:  # noqa: BLE001 — SMTP en panne : on réessaie au prochain passage
            n = (m["tentatives"] or 0) + 1
            LOG.warning("envoi impossible pour le message %s (%s), tentative %s", mid, type(e).__name__, n)
            self.journal.mettre_a_jour(mid, tentatives=n, statut="echec_envoi" if n >= MAX_ENVOIS_ESSAIS else "a_notifier",
                                       detail=f"envoi en échec ({type(e).__name__})")
            return False
        self.journal.mettre_a_jour(mid, statut="notifie", notifie_le=maintenant_iso(self.maintenant()), destinataires=",".join(envoyes))
        return True

    def reprendre_notifications(self) -> int:
        n = 0
        for m in self.journal.en_attente():
            n += bool(self._notifier(m["id"]))
        return n

    def alerter_admin(self, cle: str, sujet: str, corps: str) -> None:
        """Alerte technique (jamais de contenu de message), au plus une fois par 24 h et par motif."""
        meta = "admin-" + cle
        if self.journal.meta(meta, "")[:10] == maintenant_iso(self.maintenant())[:10]:
            return
        try:
            self.envoyeur.envoyer(self.cfg.dest_defaut, f"[Standard ABI] {sujet}", f"{corps}\n\n{self.pied()}")
            self.journal.definir_meta(meta, maintenant_iso(self.maintenant()))
        except Exception as e:  # noqa: BLE001
            LOG.warning("alerte technique non envoyée (%s)", type(e).__name__)

    # ------------------------------------------------------------------ texte de la synthèse
    def _corps_synthese(self, s: Synthese, texte, local, numero, source, qui, tronque, duree_s, dest, tid, echeance) -> str:
        urgent = s.urgence == 5
        hors = self._hors_horaires()
        L = []
        if urgent:
            L += ["!!! URGENCE 5/5 — ALERTE IMMÉDIATE : à traiter tout de suite par une personne !!!",
                  f"Mots d'alerte détectés : {', '.join(s.mots_alerte)}" if s.mots_alerte else "Classé en urgence maximale.", ""]
        L += [f"Message vocal reçu le {jour_fr(local)} (heure de Paris){', hors horaires d’ouverture' if hors else ''}"
              + (f", durée {int(duree_s)} s." if duree_s else "."), "",
              "QUI APPELLE",
              f"  Nom : {s.nom or 'non précisé'}   ·   Société : {s.societe or 'non précisée'}",
              f"  Numéro à rappeler : {formater_numero(numero) if numero else 'inconnu'}  ({source})", "",
              f"POURQUOI : {s.motif}",
              f"CATÉGORIE : {CATEGORIES_LISIBLES[s.categorie]}   ·   AGENT CONCERNÉ : {AGENTS_LISIBLES.get(s.agent, s.agent)}   ·   URGENCE : {s.urgence}/5",
              "FEU : " + ("ROUGE — rien n'est parti ; la suite est une décision humaine." if urgent
                          else "VERT — synthèse interne ; tout rappel au client reste une action humaine."), "",
              "RÉSUMÉ", *[f"  {x}" for x in s.resume], "", f"ACTION PROPOSÉE : {s.action}"]
        if tid:
            L.append(f"TÂCHE DE RAPPEL n°{tid} pour {dest[0]}, à faire avant le {jour_fr(echeance.astimezone(self.tz))}.")
        if tronque:
            L += ["", f"ATTENTION : message long, seules les {self.cfg.duree_max_s} premières secondes ont été transcrites. "
                      "Écouter l'original dans la boîte vocale de l'opérateur."]
        if s.classement == "ia" and not s.resume_ok:
            L += ["", "Note : la Secrétaire n'a pas fourni de vrai résumé ; ce résumé a été reconstitué à partir du motif et de l'action. Se fier à la transcription."]
        if s.classement == "regles":
            L += ["", "Note : classement de secours (règles simples), la Secrétaire n'a pas répondu. À relire avec attention."]
        L += ["", "TRANSCRIPTION AUTOMATIQUE (peut contenir des erreurs ; l'enregistrement d'origine reste dans la boîte vocale de l'opérateur)",
              "> " + texte, "", self.pied()]
        return "\n".join(L)

    def pied(self) -> str:
        return ("—\nCe message a été préparé par le standard automatique d'ABI Holding. Il n'a PAS rappelé ni répondu à l'appelant : "
                "le rappel est fait par une personne.\nL'audio est supprimé du serveur après transcription ; la transcription est conservée "
                f"{self.cfg.conservation_jours} jours.")
