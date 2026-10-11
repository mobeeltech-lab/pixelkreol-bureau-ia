"""Transcription LOCALE (faster-whisper, CPU, int8). Aucune voix ne quitte le serveur.

Le modèle est chargé à la demande et libéré quand la file est vide (réglable) : au repos le service ne garde aucun modèle en mémoire.
Une seule transcription à la fois (le pipeline est séquentiel).
"""
from __future__ import annotations

import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import audio

LOG = logging.getLogger("abi_standard.transcription")

# Phrases que Whisper « invente » sur du silence ou du bruit (artefacts connus) : jamais un vrai message.
HALLUCINATIONS = ("sous-titres realises par", "sous-titrage", "amara.org", "merci d'avoir regarde", "merci de votre attention",
                  "abonnez-vous", "a bientot pour une nouvelle video", "sous-titres par la communaute")


@dataclass
class Transcription:
    texte: str
    duree_s: float | None
    tronque: bool
    confiance: float | None
    secondes_calcul: float


class Transcripteur(Protocol):
    def transcrire(self, chemin: Path, duree_max_s: int) -> Transcription: ...
    def liberer(self) -> None: ...


def nettoyer(texte: str) -> str:
    return re.sub(r"\s+", " ", texte or "").strip()


def compter_mots(texte: str) -> int:
    return len(re.findall(r"[A-Za-zÀ-ÿ0-9]+", texte or ""))


def est_hallucination(texte: str) -> bool:
    t = (texte or "").lower().replace("’", "'")
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    return any(h in t for h in HALLUCINATIONS)


class FasterWhisper:
    def __init__(self, modele: str, dossier: str, threads: int = 2, beam: int = 1, telechargement: bool = False):
        self.modele, self.dossier, self.threads, self.beam = modele, dossier, threads, beam
        self.telechargement = telechargement          # False en service : le modèle doit déjà être sur le disque
        self._m = None

    def _charger(self):
        if self._m is None:
            from faster_whisper import WhisperModel
            debut = time.monotonic()
            self._m = WhisperModel(self.modele, device="cpu", compute_type="int8", cpu_threads=self.threads,
                                   num_workers=1, download_root=self.dossier, local_files_only=not self.telechargement)
            LOG.info("modèle %s chargé en %.1f s", self.modele, time.monotonic() - debut)
        return self._m

    def transcrire(self, chemin: Path, duree_max_s: int) -> Transcription:
        debut = time.monotonic()
        pcm, total, tronque = audio.decoder(chemin, duree_max_s)
        modele = self._charger()
        segments, _info = modele.transcribe(pcm, language="fr", beam_size=self.beam, vad_filter=True,
                                            vad_parameters={"min_silence_duration_ms": 500}, temperature=0.0,
                                            condition_on_previous_text=False, no_speech_threshold=0.6)
        morceaux, confs = [], []
        for s in segments:                                  # le générateur travaille ici
            if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                continue
            morceaux.append(s.text)
            confs.append(math.exp(min(0.0, s.avg_logprob)))
        return Transcription(nettoyer(" ".join(morceaux)), total if total is not None else len(pcm) / audio.FREQ,
                             tronque, (sum(confs) / len(confs)) if confs else None, time.monotonic() - debut)

    def liberer(self) -> None:
        self._m = None
        import gc
        gc.collect()
