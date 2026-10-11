"""Tri des pièces jointes et lecture bornée de l'audio (jamais plus de `max_s` secondes en mémoire)."""
from __future__ import annotations

import wave
from pathlib import Path

EXTENSIONS_AUDIO = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".amr", ".3gp", ".flac", ".wma", ".mp4"}
# Début de fichier qui prouve que ce n'est PAS de l'audio, quoi que dise le nom ou le type annoncé.
SIGNATURES_NON_AUDIO = (b"MZ", b"%PDF", b"PK\x03\x04", b"\x7fELF", b"<html", b"<!DOC", b"<?xml", b"#!", b"\xd0\xcf\x11\xe0", b"Rar!")
FREQ = 16000


class ErreurAudio(Exception):
    """Fichier audio illisible ou inexploitable (message sans donnée personnelle)."""


def extension(nom: str) -> str:
    return Path(nom or "").suffix.lower()


def annonce_audio(type_mime: str, nom: str) -> bool:
    """Le mail DIT-il que c'est de l'audio ? (type MIME audio/*, ou nom en .wav/.mp3/… quand le type est générique)."""
    t = (type_mime or "").lower()
    if t.startswith("audio/") or t in ("video/3gpp", "application/ogg"):
        return True
    return t in ("", "application/octet-stream", "binary/octet-stream") and extension(nom) in EXTENSIONS_AUDIO


def semble_non_audio(debut: bytes) -> bool:
    d = (debut or b"").lstrip()[:16]
    return any(d.startswith(s) for s in SIGNATURES_NON_AUDIO)


def duree(chemin: Path) -> float | None:
    """Durée en secondes sans décoder le fichier, ou None si on ne sait pas la lire."""
    try:
        with open(chemin, "rb") as f:
            entete = f.read(12)
        if entete[:4] == b"RIFF" and entete[8:12] == b"WAVE":
            with wave.open(str(chemin), "rb") as w:
                return w.getnframes() / float(w.getframerate() or 1)
    except (OSError, EOFError, wave.Error):
        pass
    try:
        import av
        with av.open(str(chemin)) as c:
            if c.duration:
                return c.duration / 1_000_000.0
    except Exception:  # noqa: BLE001 — fichier cassé : on le dira plus tard, proprement
        return None
    return None


def decoder(chemin: Path, max_s: float):
    """Décode en 16 kHz mono (float32) en s'ARRÊTANT à `max_s` secondes. Renvoie (échantillons, durée totale ou None, tronqué)."""
    import numpy as np
    try:
        import av
    except ImportError as e:  # pragma: no cover
        raise ErreurAudio("décodeur audio absent") from e
    limite = int(max_s * FREQ)
    morceaux, n, tronque = [], 0, False
    try:
        with av.open(str(chemin)) as c:
            flux = next((s for s in c.streams if s.type == "audio"), None)
            if flux is None:
                raise ErreurAudio("aucune piste audio dans le fichier")
            total = c.duration / 1_000_000.0 if c.duration else None
            conv = av.AudioResampler(format="s16", layout="mono", rate=FREQ)
            for trame in c.decode(flux):
                for f in conv.resample(trame):
                    a = f.to_ndarray().reshape(-1)
                    if n + len(a) >= limite:
                        morceaux.append(a[: limite - n])
                        n = limite
                        tronque = True
                        break
                    morceaux.append(a)
                    n += len(a)
                if tronque:
                    break
    except ErreurAudio:
        raise
    except Exception as e:  # noqa: BLE001 — toute erreur de décodage = fichier illisible
        raise ErreurAudio(f"fichier audio illisible ({type(e).__name__})") from e
    if n == 0:
        raise ErreurAudio("fichier audio vide")
    pcm = np.concatenate(morceaux).astype(np.float32) / 32768.0
    return pcm, total, tronque
