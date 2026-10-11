"""Mesures du standard téléphonique, sur ce que l'environnement permet (sans le modèle Whisper, non téléchargeable ici).

    taskset -c 0,1 python tests/mesure_standard.py            # limite à 2 processeurs, comme le VPS

Mesure : (1) lecture d'un message d'UNE minute par le vrai décodeur (temps, mémoire) ; (2) passage de 8 messages fictifs de bout en bout,
faux moteur de transcription (coût du reste du programme : IMAP, classement, SMTP, journal) ; (3) un message d'UNE HEURE : la lecture reste bornée.
Le temps de transcription lui-même se mesure avec `python -m abi_standard.mesure` sur la machine cible (voir STANDARD-LISEZMOI.md).
"""
from __future__ import annotations

import os
import resource
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "instances" / "abi" / "standard"))
import fabrique_standard as fs  # noqa: E402
import fabrique_voix  # noqa: E402

from abi_standard import audio  # noqa: E402
from abi_standard.config import Config  # noqa: E402
from abi_standard.mesure import repeter, rss_mo  # noqa: E402
from abi_standard.pipeline import Standard  # noqa: E402


def pic_mo() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main() -> None:
    print(f"processeurs utilisables : {len(os.sched_getaffinity(0))}  ·  mémoire au départ : {rss_mo():.0f} Mo")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        voix = {m.cle: fabrique_voix.parler(d / f"{m.cle}.wav", m.dit) for m in fs.MESSAGES}
        # (1) une minute d'audio, décodée par le vrai décodeur
        minute = d / "minute.wav"
        dur = repeter(voix["vente"], minute, 60)
        avant = rss_mo()
        t0 = time.monotonic()
        pcm, total, tronque = audio.decoder(minute, 180)
        t1 = time.monotonic() - t0
        print(f"(1) décodage d'un message de {dur:.0f} s ({minute.stat().st_size / 1e6:.1f} Mo, WAV 16 kHz) : {t1 * 1000:.0f} ms, "
              f"échantillons 16 kHz mono = {pcm.nbytes / 1e6:.1f} Mo, mémoire +{rss_mo() - avant:.0f} Mo")
        # (2) 8 messages de bout en bout
        boite, smtp_boite = fs.BoiteIMAP(), fs.BoiteSMTP()
        imap, smtp = fs.ServeurIMAP(boite), fs.ServeurSMTP(smtp_boite)
        (d / "tmp").mkdir()
        cfg = Config.depuis_env(fs.env_standard(imap.port, smtp.port, d))
        trans, secr = fs.FauxTranscripteur(), fs.FausseSecretaire()
        for m in fs.MESSAGES:
            trans.enregistrer(voix[m.cle], m.dit)
            boite.deposer(fs.fabriquer_mail(voix[m.cle], numero=m.numero_entete))
        std = Standard(cfg, trans, client_http=secr.client())
        t0 = time.monotonic()
        st = std.cycle()
        t2 = time.monotonic() - t0
        print(f"(2) 8 messages de bout en bout (faux moteur, fausse Secrétaire) : {t2:.2f} s, {st['traites']} traités, "
              f"{len(smtp_boite.recus)} synthèses, pic de mémoire {pic_mo():.0f} Mo")
        imap.arreter()
        smtp.arreter()
        # (3) un fichier absurde (1 heure) : la lecture reste bornée
        long = fabrique_voix.ton(d / "long.wav", 3600, 440, 8000)
        avant = rss_mo()
        t0 = time.monotonic()
        pcm, total, tronque = audio.decoder(long, 180)
        print(f"    message d'UNE HEURE, borné à 180 s : {1000 * (time.monotonic() - t0):.0f} ms, +{rss_mo() - avant:.0f} Mo, tronqué={tronque}")


if __name__ == "__main__":
    main()
