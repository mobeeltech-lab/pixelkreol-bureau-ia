"""Fabrique de voix FICTIVES pour les tests du standard téléphonique (aucune vraie voix, aucun vrai numéro).

Synthèse vocale : espeak-ng, fourni par le paquet PyPI `espeakng-loader` (bibliothèque et voix embarquées,
aucun service extérieur, aucun téléchargement de modèle). Si le paquet est absent, `voix_disponible()` renvoie
False et les tests qui en ont besoin sont ignorés : les autres tests n'en dépendent pas.
"""
from __future__ import annotations

import ctypes
import struct
import wave
from pathlib import Path


def voix_disponible() -> bool:
    try:
        import espeakng_loader  # noqa: F401
        return True
    except ImportError:
        return False


_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)
_etat: dict = {}


def _initialiser():
    if _etat:
        return _etat
    import espeakng_loader
    lib = ctypes.CDLL(espeakng_loader.get_library_path())
    lib.espeak_Initialize.restype = ctypes.c_int
    freq = lib.espeak_Initialize(1, 0, espeakng_loader.get_data_path().encode(), 0)   # 1 = récupération des échantillons
    if freq <= 0:
        raise RuntimeError("espeak-ng ne démarre pas")
    lib.espeak_SetVoiceByName(b"fr")
    morceaux: list[bytes] = []

    @_CB
    def rappel(wav, n, ev):
        if wav and n > 0:
            morceaux.append(ctypes.string_at(wav, n * 2))
        return 0
    lib.espeak_SetSynthCallback(rappel)
    _etat.update(lib=lib, freq=freq, morceaux=morceaux, rappel=rappel)
    return _etat


def synthetiser(texte: str, vitesse: int = 150) -> tuple[bytes, int]:
    """Renvoie (échantillons 16 bits mono, fréquence)."""
    e = _initialiser()
    lib = e["lib"]
    lib.espeak_SetParameter(1, vitesse, 0)           # espeakRATE
    e["morceaux"].clear()
    brut = texte.encode("utf-8") + b"\0"
    lib.espeak_Synth(brut, len(brut), 0, 1, 0, 1, None, None)
    lib.espeak_Synchronize()
    return b"".join(e["morceaux"]), e["freq"]


def ecrire_wav(chemin: Path, pcm: bytes, freq: int) -> Path:
    with wave.open(str(chemin), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(freq)
        w.writeframes(pcm)
    return chemin


def parler(chemin: Path, texte: str, vitesse: int = 150) -> Path:
    pcm, freq = synthetiser(texte, vitesse)
    return ecrire_wav(chemin, pcm, freq)


def silence(chemin: Path, secondes: float, freq: int = 8000) -> Path:
    return ecrire_wav(chemin, b"\0\0" * int(secondes * freq), freq)


def bruit_leger(chemin: Path, secondes: float, freq: int = 8000) -> Path:
    """Souffle déterministe (pseudo-aléatoire figé) : un message « inaudible »."""
    x, pcm = 12345, bytearray()
    for _ in range(int(secondes * freq)):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        pcm += struct.pack("<h", ((x >> 16) % 600) - 300)
    return ecrire_wav(chemin, bytes(pcm), freq)


def ton(chemin: Path, secondes: float, hertz: float = 440.0, freq: int = 8000) -> Path:
    import math
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * hertz * i / freq))) for i in range(int(secondes * freq)))
    return ecrire_wav(chemin, pcm, freq)
