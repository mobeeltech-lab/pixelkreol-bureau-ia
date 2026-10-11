"""Mesure du temps et de la mémoire de la transcription sur CETTE machine (à lancer sur le VPS, sous les mêmes limites que le service).

    docker compose run --rm abi_standard python -m abi_standard.mesure [--audio fichier.wav] [--secondes 60]

Sans --audio, une voix de synthèse (espeak-ng, si le paquet espeakng-loader est installé) est fabriquée. Le fichier est répété jusqu'à
atteindre la durée demandée. Aucune voix réelle n'est nécessaire.
"""
from __future__ import annotations

import argparse
import os
import resource
import sys
import tempfile
import time
import wave
from pathlib import Path

from . import audio
from .config import Config
from .transcription import FasterWhisper


def rss_mo() -> float:
    with open("/proc/self/status") as f:
        for l in f:
            if l.startswith("VmRSS:"):
                return int(l.split()[1]) / 1024
    return 0.0


def pic_mo() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def repeter(source: Path, cible: Path, secondes: float) -> float:
    """Écrit dans `cible` (WAV 16 kHz mono) le contenu de `source` (WAV, MP3, M4A…) répété jusqu'à `secondes`."""
    import numpy as np
    pcm, _total, _tronque = audio.decoder(source, 600)
    brut = (pcm * 32767).astype(np.int16)
    voulu = int(secondes * audio.FREQ)
    corps = np.tile(brut, voulu // len(brut) + 1)[:voulu]
    with wave.open(str(cible), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(audio.FREQ)
        w.writeframes(corps.tobytes())
    return len(corps) / audio.FREQ


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--audio")
    p.add_argument("--secondes", type=float, default=60)
    p.add_argument("--modele")
    a = p.parse_args(argv)
    cfg = Config.depuis_env()
    modele = a.modele or cfg.whisper_modele
    with tempfile.TemporaryDirectory() as d:
        src = Path(a.audio) if a.audio else Path(d) / "voix.wav"
        if not a.audio:
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tests"))     # dépôt complet seulement (pas dans le conteneur)
                import fabrique_voix
                fabrique_voix.parler(src, "Bonjour, ici Monsieur Martin de la société Exemple. Je vous appelle au sujet de l'appartement "
                                          "du programme les Oliviers. Pouvez-vous me rappeler demain matin au zéro six trente-neuf quatre-vingt-dix-huit "
                                          "douze trente-quatre. Merci et bonne journée.")
            except Exception as e:  # noqa: BLE001
                print("Pas de voix de synthèse disponible ; donnez un fichier avec --audio.", type(e).__name__)
                return 2
        cible = Path(d) / "mesure.wav"
        duree = repeter(src, cible, a.secondes)
        print(f"modèle={modele} threads={cfg.threads} beam={cfg.beam} audio={duree:.0f} s CPU disponibles={os.cpu_count()}")
        print(f"mémoire avant chargement : {rss_mo():.0f} Mo")
        t = FasterWhisper(modele, cfg.whisper_dossier, cfg.threads, cfg.beam)
        d0 = time.monotonic()
        t._charger()
        print(f"chargement du modèle : {time.monotonic() - d0:.1f} s, mémoire {rss_mo():.0f} Mo")
        d0 = time.monotonic()
        r = t.transcrire(cible, int(duree) + 5)
        tc = time.monotonic() - d0
        print(f"transcription : {tc:.1f} s pour {duree:.0f} s d'audio (×{duree / tc:.2f} le temps réel), pic mémoire {pic_mo():.0f} Mo")
        print(f"texte ({len(r.texte.split())} mots) : {r.texte[:300]}")
        t.liberer()
        print(f"mémoire après libération du modèle : {rss_mo():.0f} Mo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
