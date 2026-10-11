"""Commandes : python -m abi_standard <commande>   (voir STANDARD-LISEZMOI.md)"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

from .config import Config
from .journal import Journal
from .pipeline import Standard
from .classement import formater_numero
from .transcription import FasterWhisper

LOG = logging.getLogger("abi_standard")
VIVANT = "standard-vivant"          # fichier « battement de cœur » (dans le dossier temporaire du conteneur, en mémoire)


def construire(cfg: Config) -> Standard:
    t = FasterWhisper(cfg.whisper_modele, cfg.whisper_dossier, cfg.threads, cfg.beam)
    return Standard(cfg, t)


def cmd_servir(cfg: Config, _a) -> int:
    if not cfg.actif:
        print("Standard téléphonique désactivé (ABI_STANDARD_ACTIF=0) : rien à faire.")
        return 0
    pb = cfg.problemes()
    if pb:
        print("Standard NON démarré, réglages incomplets :\n - " + "\n - ".join(pb), file=sys.stderr)
        return 2
    s = construire(cfg)
    s.nettoyer_tmp()
    arret = {"oui": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: arret.update(oui=True))
    LOG.info("standard démarré : modèle %s, relève toutes les %s s, un message à la fois", cfg.whisper_modele, cfg.intervalle_s)
    while not arret["oui"]:
        try:
            st = s.cycle()
        except Exception:  # noqa: BLE001 — la boucle ne doit jamais mourir
            LOG.exception("passage interrompu par une erreur inattendue")
            st = {"panne": "erreur inattendue", "traites": 0}
        Path(cfg.dossier_tmp, VIVANT).write_text(str(int(time.time())))
        if st.get("traites") or st.get("notifies") or st.get("panne"):
            LOG.info("passage : %s", {k: v for k, v in st.items() if v})
        pause = 1 if st.get("traites", 0) >= cfg.lot_max and not st.get("panne") else cfg.intervalle_s
        for _ in range(int(pause * 2)):
            if arret["oui"]:
                break
            time.sleep(0.5)
    LOG.info("standard arrêté proprement")
    return 0


def cmd_une_fois(cfg: Config, _a) -> int:
    pb = cfg.problemes()
    if pb:
        print("Réglages incomplets :\n - " + "\n - ".join(pb), file=sys.stderr)
        return 2
    s = construire(cfg)
    s.nettoyer_tmp()
    print(s.cycle())
    return 0


def cmd_sante(cfg: Config, _a) -> int:
    if not cfg.actif:
        return 0
    f = Path(cfg.dossier_tmp, VIVANT)
    try:
        return 0 if time.time() - int(f.read_text()) < max(300, cfg.intervalle_s * 4 + cfg.orch_delai * 2 * cfg.lot_max) else 1
    except (OSError, ValueError):
        return 1


def cmd_telecharger(cfg: Config, _a) -> int:
    """À faire UNE fois (le seul moment où le service parle à Internet, pour récupérer le modèle de transcription)."""
    print(f"Téléchargement du modèle « {cfg.whisper_modele} » dans {cfg.whisper_dossier} …")
    FasterWhisper(cfg.whisper_modele, cfg.whisper_dossier, cfg.threads, cfg.beam, telechargement=True)._charger()
    print("Modèle prêt. Le service ne se connectera plus à Internet pour la voix.")
    return 0


def cmd_taches(cfg: Config, a) -> int:
    j = Journal(cfg.base)
    lignes = j.taches(faites=a.faites)
    if not lignes:
        print("Aucune tâche." if not a.faites else "Aucune tâche faite.")
    for t in lignes:
        print(f"#{t['id']:<4} avant {t['echeance']}  {t['destinataire']:<32} {t['libelle']}"
              + (f"  [{formater_numero(t['numero_rappel'])}]" if t["numero_rappel"] else ""))
    return 0


def cmd_fait(cfg: Config, a) -> int:
    ok = Journal(cfg.base).tache_faite(a.numero)
    print("Tâche marquée faite." if ok else "Tâche introuvable ou déjà faite.")
    return 0 if ok else 1


def cmd_journal(cfg: Config, a) -> int:
    for m in Journal(cfg.base).liste(a.jours):
        print(f"{m['recu_le']}  {m['numero_masque']:<16} {m['categorie'] or '-':<15} urgence {m['urgence'] or '-'}  "
              f"{m['statut']:<15} {m['destinataires']}  {m['detail']}")
    return 0


def cmd_etat(cfg: Config, _a) -> int:
    print(f"actif={cfg.actif} modèle={cfg.whisper_modele} conservation={cfg.conservation_jours} j")
    print("Messages par statut :", Journal(cfg.base).compter_par_statut() or "aucun")
    pb = cfg.problemes()
    print("Réglages : " + ("complets" if not pb else "INCOMPLETS\n - " + "\n - ".join(pb)))
    return 0


def cmd_purger(cfg: Config, _a) -> int:
    print(Journal(cfg.base).purger(cfg.conservation_jours, cfg.conservation_journal_jours))
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=os.getenv("ABI_STANDARD_LOG", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="abi_standard")
    sub = p.add_subparsers(dest="cmd", required=True)
    for nom, f, aide in (("servir", cmd_servir, "boucle de relève (démarrage du conteneur)"), ("une-fois", cmd_une_fois, "un seul passage"),
                         ("sante", cmd_sante, "contrôle de santé du conteneur"), ("telecharger", cmd_telecharger, "récupérer le modèle de voix"),
                         ("etat", cmd_etat, "résumé"), ("purger", cmd_purger, "appliquer la durée de conservation")):
        sub.add_parser(nom, help=aide).set_defaults(f=f)
    t = sub.add_parser("taches", help="tâches de rappel à faire")
    t.add_argument("--faites", action="store_true")
    t.set_defaults(f=cmd_taches)
    ft = sub.add_parser("fait", help="marquer une tâche faite")
    ft.add_argument("numero", type=int)
    ft.set_defaults(f=cmd_fait)
    jr = sub.add_parser("journal", help="journal des messages (numéros masqués)")
    jr.add_argument("--jours", type=int, default=7)
    jr.set_defaults(f=cmd_journal)
    a = p.parse_args(argv)
    return a.f(Config.depuis_env(), a)


if __name__ == "__main__":
    sys.exit(main())
