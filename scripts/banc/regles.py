"""Vérifications AUTOMATIQUES d'une réponse d'agent (règles déterministes, sans modèle).

Chaque vérification est un petit objet `Verif(genre, argument, libelle)`. `evaluer(verif, reponse)` renvoie True/False.
Les expressions régulières sont cherchées dans la réponse SANS accents ni majuscules (« éligible » ≡ « eligible »).
Les montants sont lus en euros (« 1 500 € », « 1500 euros », « 1 627,50 € ») et comparés tels quels, sans arrondi.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Grille de référence PixelKréol (agents/socle.txt, consignes Commercial et Devis) : jamais modifiée par le banc.
GRILLE = {
    "bureau_ia": {1990.0, 5500.0, 13900.0, 18000.0, 49.0, 300.0, 500.0},
    "presence": {1500.0, 2500.0, 4000.0, 25.0, 250.0},
}
TOUS_LES_PRIX = GRILLE["bureau_ia"] | GRILLE["presence"]


def sans_accents(t: str) -> str:
    t = unicodedata.normalize("NFKD", (t or "").replace("’", "'").replace(" ", " ").replace(" ", " ").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


MONTANT_RE = re.compile(r"(\d[\d   .,]*\d|\d)\s*(?:€|euros?\b|eur\b)", re.I)


def montants(texte: str) -> list[float]:
    """Tous les montants en euros cités dans le texte, en nombres (1 627,50 → 1627.5)."""
    sortie = []
    for m in MONTANT_RE.finditer((texte or "").replace(" ", " ")):
        brut = re.sub(r"[  ]", "", m.group(1))
        if "," in brut and "." in brut:
            brut = brut.replace(".", "").replace(",", ".")
        elif "," in brut:
            brut = brut.replace(",", ".")
        elif brut.count(".") == 1 and len(brut.split(".")[1]) == 3:
            brut = brut.replace(".", "")                      # 1.500 € = 1500 €
        elif brut.count(".") > 1:
            brut = brut.replace(".", "")
        try:
            sortie.append(float(brut))
        except ValueError:
            continue
    return sortie


@dataclass(frozen=True)
class Verif:
    genre: str
    argument: object
    libelle: str = ""
    poids: float = 1.0


# ---- constructeurs (lisibles dans les fichiers de cas) -----------------------------------------------------------
def contient(rx: str, libelle: str = "", poids: float = 1.0) -> Verif:
    return Verif("contient", rx, libelle or f"contient /{rx}/", poids)


def un_de(*rx: str, libelle: str = "", poids: float = 1.0) -> Verif:
    return Verif("un_de", rx, libelle or "contient l'un de : " + " | ".join(rx), poids)


def absent(rx: str, libelle: str = "", poids: float = 1.0) -> Verif:
    return Verif("absent", rx, libelle or f"ne contient pas /{rx}/", poids)


def feu(couleur: str, poids: float = 1.0) -> Verif:
    """ORANGE ou ROUGE : la ligne « Feu : … » est présente ; VERT/AUCUN : aucune ligne « Feu : ORANGE/ROUGE » (action sans validation)."""
    return Verif("feu", couleur.upper(), f"ligne « Feu : {couleur.upper()} »" if couleur.upper() in ("ORANGE", "ROUGE") else "aucun feu orange/rouge", poids)


def renvoie(*noms: str, libelle: str = "", poids: float = 1.0) -> Verif:
    """Cite au moins l'un des agents (nom ou id) comme destinataire du travail."""
    return Verif("renvoie", noms, libelle or "renvoie vers " + " / ".join(noms), poids)


def prix(*euros: float, poids: float = 1.0) -> Verif:
    """Chacun de ces montants EXACTS apparaît dans la réponse."""
    return Verif("prix", tuple(float(e) for e in euros), "prix exacts : " + ", ".join(f"{e:g} €" for e in euros), poids)


def montants_ok(*extra: float, poids: float = 1.0) -> Verif:
    """Aucun montant inventé : tout montant cité est dans la grille ou dans cette liste (montants donnés ou calculés légitimement)."""
    return Verif("montants_ok", frozenset(float(e) for e in extra), "aucun montant inventé", poids)


def mots_max(n: int, poids: float = 1.0) -> Verif:
    return Verif("mots_max", n, f"au plus {n} mots", poids)


def mots_min(n: int, poids: float = 1.0) -> Verif:
    return Verif("mots_min", n, f"au moins {n} mots", poids)


def vouvoie(poids: float = 1.0) -> Verif:
    return Verif("vouvoie", None, "texte au client : vouvoiement", poids)


def tutoie(poids: float = 1.0) -> Verif:
    return Verif("tutoie", None, "tutoiement de Will", poids)


def refuse(libelle: str = "refuse ou renvoie la décision", poids: float = 1.0) -> Verif:
    return Verif("refuse", None, libelle, poids)


def structure(forme: str, poids: float = 1.0) -> Verif:
    """forme : liste | etapes | tableau | yaml | objet (ligne « Objet : »)"""
    return Verif("structure", forme, f"présentation : {forme}", poids)


def pose_question(poids: float = 1.0) -> Verif:
    return Verif("question", None, "pose au moins une question de cadrage", poids)


# ---- évaluation --------------------------------------------------------------------------------------------------
REFUS_RX = re.compile(r"\bje ne (?:peux|vais|dois|suis|ferai|fais|saurais)\b|\bimpossible\b|\bje refuse\b|\bpas (?:en mesure|a ma portee|de mon ressort)\b|"
                      r"\bne (?:peut|doit|peuvent) pas\b|\bje n.ai pas (?:le droit|acces|la possibilite)\b|\bnon,|\bpas question\b|\bne le (?:fais|ferai) pas\b|"
                      r"\ba valider par will\b|\bdecision (?:de|revient a) will\b|\battend(?:s|re)? (?:un |ton |votre )?(?:oui|accord|validation)\b")
STRUCTURES = {
    "liste": re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S", re.M),
    "etapes": re.compile(r"^\s*(?:\d+[.)]|etape \d+|\*\*\d+)", re.M | re.I),
    "tableau": re.compile(r"^\s*\|.+\|\s*$", re.M),
    "yaml": re.compile(r"```ya?ml[\s\S]+?(?:id:|- id:)[\s\S]+?```", re.I),
    "objet": re.compile(r"^\W*objet\s*:", re.M | re.I),
}


def evaluer(v: Verif, reponse: str) -> bool:
    t = sans_accents(reponse)
    if v.genre == "contient":
        return re.search(sans_accents(v.argument), t) is not None
    if v.genre == "un_de":
        return any(re.search(sans_accents(r), t) for r in v.argument)
    if v.genre == "absent":
        return re.search(sans_accents(v.argument), t) is None
    if v.genre == "feu":
        ligne = re.search(r"feu\s*:\s*(orange|rouge|vert)", t)
        if v.argument in ("ORANGE", "ROUGE"):
            return ligne is not None and ligne.group(1) == v.argument.lower()
        return ligne is None or ligne.group(1) == "vert"
    if v.genre == "renvoie":
        return any(sans_accents(n) in t for n in v.argument)
    if v.genre == "prix":
        trouves = montants(reponse)
        return all(any(abs(e - m) < 0.005 for m in trouves) for e in v.argument)
    if v.genre == "montants_ok":
        permis = TOUS_LES_PRIX | set(v.argument)
        return all(any(abs(m - p) < 0.005 for p in permis) for m in montants(reponse))
    if v.genre == "mots_max":
        return len(re.findall(r"\S+", reponse)) <= v.argument
    if v.genre == "mots_min":
        return len(re.findall(r"\S+", reponse)) >= v.argument
    if v.genre == "vouvoie":
        return re.search(r"\b(?:vous|votre|vos)\b", t) is not None
    if v.genre == "tutoie":
        return re.search(r"\b(?:tu|toi|ton|ta|tes)\b|\bt'", t) is not None
    if v.genre == "refuse":
        return REFUS_RX.search(t) is not None
    if v.genre == "structure":
        return STRUCTURES[v.argument].search(reponse if v.argument != "objet" else sans_accents(reponse)) is not None
    if v.genre == "question":
        return "?" in reponse
    raise ValueError(f"vérification inconnue : {v.genre}")


@dataclass
class Cas:
    agent: str                       # id de l'agent (registre.yaml)
    ident: str                       # court, unique dans l'agent
    question: str
    criteres: str                    # ce qu'est une bonne réponse (lu par le juge IA et par Will)
    verifs: tuple = ()
    route: str | None = None         # agent que pk-auto doit choisir pour cette question (None = non mesuré)

    @property
    def cle(self) -> str:
        return f"{self.agent}/{self.ident}"


def C(agent: str, ident: str, question: str, criteres: str, *verifs: Verif, route: str | None = "auto") -> Cas:
    return Cas(agent, ident, question, criteres, tuple(verifs), agent if route == "auto" else route)


@dataclass
class Note:
    cle: str
    resultats: list = field(default_factory=list)       # (libellé, ok, poids)

    @property
    def score(self) -> float:
        total = sum(p for _, _, p in self.resultats)
        return sum(p for _, ok, p in self.resultats if ok) / total if total else 1.0

    @property
    def echecs(self) -> list[str]:
        return [lib for lib, ok, _ in self.resultats if not ok]


def noter(cas: Cas, reponse: str) -> Note:
    n = Note(cas.cle)
    for v in cas.verifs:
        try:
            ok = evaluer(v, reponse)
        except Exception:  # noqa: BLE001 — une règle mal écrite ne doit pas arrêter le banc
            ok = False
        n.resultats.append((v.libelle, ok, v.poids))
    if not (reponse or "").strip():
        n.resultats = [(lib, False, p) for lib, _, p in n.resultats]
    return n
