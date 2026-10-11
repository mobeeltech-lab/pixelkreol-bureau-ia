"""Vérifications AUTOMATIQUES d'une réponse d'agent (règles déterministes, sans modèle).

Chaque vérification est un petit objet `Verif(genre, argument, libelle)`. `evaluer(verif, reponse)` renvoie True/False.
Les expressions régulières sont cherchées dans la réponse SANS accents ni majuscules (« éligible » ≡ « eligible »).
Les montants sont lus en euros (« 1 500 € », « 1500 euros », « 1 627,50 € ») et comparés tels quels, sans arrondi.
"""
from __future__ import annotations

import itertools
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

# Grille de référence PixelKréol (agents/socle.txt, consignes Commercial et Devis) : jamais modifiée par le banc.
GRILLE = {
    "bureau_ia": {1990.0, 5500.0, 13900.0, 18000.0, 49.0, 300.0, 500.0},
    "presence": {1500.0, 2500.0, 4000.0, 25.0, 250.0},
    "devis": {600.0},                      # taux journalier par défaut de l'agent Devis (€ HT par jour)
}
TOUS_LES_PRIX = GRILLE["bureau_ia"] | GRILLE["presence"] | GRILLE["devis"]


def sans_accents(t: str) -> str:
    t = unicodedata.normalize("NFKD", (t or "").replace("’", "'").replace(" ", " ").replace(" ", " ").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


ESPACES_SPECIAUX = re.compile(r"[\u00a0\u2000-\u200a\u202f\u205f\u3000]")


def nettoyer(texte: str) -> str:
    """Rend une réponse comparable : espaces insécables / fines → espace ; notation LaTeX (« $1\\,200 + 1\\,500 = 2\\,700$ », « 25\\,\\% », « \\times »,
    « \\text{€} ») → texte simple. Appliqué à TOUTES les vérifications avant de comparer."""
    t = ESPACES_SPECIAUX.sub(" ", texte or "")
    t = re.sub(r"\\(?:text|textbf|mathrm|mathbf|textit|mathit)\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\left|\\right", "", t)
    t = t.replace("{,}", ",").replace("\\%", "%").replace("\\euro", "€").replace("\\€", "€").replace("\\_", "_").replace("\\&", "&")
    t = re.sub(r"\\(?:times|cdot)\b", "×", t)
    t = re.sub(r"\\div\b", "÷", t)
    t = re.sub(r"\\(?:approx|simeq)\b", "≈", t)
    t = re.sub(r"\\(?:rightarrow|to|Rightarrow)\b", "→", t)
    t = re.sub(r"\\(?:quad|qquad)\b", " ", t)
    t = re.sub(r"\\[,;:! ]", " ", t)                       # \, \; \: \! et « \ » : espaces fines de LaTeX
    t = re.sub(r"\\\(|\\\)|\\\[|\\\]", " ", t)
    t = re.sub(r"(?<!\\)\$+", " ", t)                       # délimiteurs $...$ / $$...$$
    return re.sub(r"[ \t]{2,}", " ", t)


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


NOMBRE_RE = re.compile(r"(?<![\w.,])(\d{1,3}(?:[ .,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)(?![\w])")


def valeurs_possibles(brut: str) -> list[float]:
    """Valeurs que peut désigner un nombre écrit à la française OU à l'anglaise : « 4 050 », « 4.050 », « 4,050 », « 1,00 », « 1.0 ».
    Un seul séparateur suivi de 3 chiffres est ambigu (1,350 = 1,35 ou 1350) : les deux lectures sont acceptées."""
    s = re.sub(r"[ \u00a0\u202f]", "", brut)
    sortie = []
    if re.fullmatch(r"\d+", s):
        return [float(s)]
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):                       # milliers seulement
        sortie.append(float(re.sub(r"[.,]", "", s)))
        if re.fullmatch(r"\d{1,3}[.,]\d{3}", s):                       # ... ou décimale à 3 chiffres
            sortie.append(float(s.replace(",", ".")))
        return sortie
    m = re.fullmatch(r"(\d{1,3}(?:[.,]\d{3})+)([.,])(\d+)", s)           # 1.234,56 / 1,234.56
    if m:
        return [float(re.sub(r"[.,]", "", m.group(1)) + "." + m.group(3))]
    try:
        return [float(s.replace(",", "."))]
    except ValueError:
        return []


def nombres(texte: str, pourcent: bool = False) -> list[float]:
    """Tous les nombres du texte (ou seulement ceux suivis de « % » / « pour cent » si pourcent=True), sous leurs lectures possibles.
    Les espaces insécables (U+00A0, U+202F, U+2009, U+2007) comptent comme des espaces."""
    texte = re.sub(r"[\u00a0\u2007\u2009\u202f]", " ", texte or "")           # 1 caractère pour 1 caractère : les positions restent valables
    sortie = []
    for m in NOMBRE_RE.finditer(texte):
        if pourcent and not re.match(r"\s*(?:%|pour ?cent)", texte[m.end():], re.I):
            continue
        sortie += valeurs_possibles(m.group(1))
    return sortie


TOLERANCE = 0.0051                      # arrondi au centime (27,125 → 27,13)


MOIS_USUELS = (1, 2, 3, 4, 5, 6, 9, 12, 18, 24, 36)      # durées d'abonnement courantes (mensualité × nombre de mois)
TAUX_TVA = (0.085, 0.20)                # DOM (Guadeloupe, Martinique, Réunion) / métropole et Corse


@lru_cache(maxsize=1)
def montants_derives() -> frozenset:
    """Montants qui découlent d'une opération simple sur la grille (conditions habituelles du Devis), et rien de plus :
    * totaux : un prix de la grille ; la somme de deux prix de la grille ; une mensualité × un nombre de mois usuel (25 € ou 250 € × 1, 2, 3, 4, 5, 6, 9, 12, 18, 24 ou 36) ; le taux journalier 600 € × 1 à 60 jours ;
    * puis, pour chaque total : la TVA (8,5 % ou 20 %), le TTC, et les échéances 30 % / 40 % du total HT et du TTC."""
    base = [1500.0, 2500.0, 4000.0, 25.0, 250.0]
    totaux = set(base) | {a + b for a, b in itertools.combinations(base, 2)} | {m * n for m in (25.0, 250.0) for n in MOIS_USUELS} | {600.0 * n for n in range(1, 61)}
    sortie = set()
    for x in totaux:
        ys = {x} | {x * tva for tva in TAUX_TVA} | {x * (1 + tva) for tva in TAUX_TVA}
        for y in ys:
            sortie |= {y, y * 0.3, y * 0.4}
    return frozenset(round(v, 4) for v in sortie)


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


def feu_en_tete(couleur: str = "ROUGE", poids: float = 1.0) -> Verif:
    """La PREMIÈRE ligne non vide de la réponse est « Feu : <couleur> » (emoji, gras, tirets tolérés avant)."""
    return Verif("feu_en_tete", couleur.upper(), f"« Feu : {couleur.upper()} » en première ligne", poids)


def avant(rx_avant: str, rx_apres: str, libelle: str = "", poids: float = 1.0) -> Verif:
    """rx_avant apparaît AVANT la première ligne de commande qui correspond à rx_apres (ligne commençant par la commande, `$` ou `sudo` tolérés).
    Échoue si aucune sauvegarde n'est annoncée avant la commande, ou si la commande est donnée sans rien avant ; réussit si la commande n'est jamais donnée seule…
    à condition que rx_avant soit présent."""
    return Verif("avant", (rx_avant, rx_apres), libelle or f"/{rx_avant}/ avant la commande /{rx_apres}/", poids)


def renvoie(*noms: str, libelle: str = "", poids: float = 1.0) -> Verif:
    """Cite au moins l'un des agents (nom ou id) comme destinataire du travail."""
    return Verif("renvoie", noms, libelle or "renvoie vers " + " / ".join(noms), poids)


def prix(*euros: float, poids: float = 1.0) -> Verif:
    """Chacun de ces montants EXACTS apparaît dans la réponse."""
    return Verif("prix", tuple(float(e) for e in euros), "prix exacts : " + ", ".join(f"{e:g} €" for e in euros), poids)


def montants_ok(*extra: float, poids: float = 1.0) -> Verif:
    """Aucun montant inventé : tout montant cité est dans la grille, dans cette liste (montants donnés ou calculés pour la question) ou DÉRIVÉ de la grille par une
    opération simple (mensualité × mois, TJ 600 € × jours, TVA 8,5 % / 20 %, TTC, échéances 30 / 40 %). Un montant qui coïncide par hasard avec une dérivation n'est pas
    détecté par cette règle seule : prix() reste le garde-fou des prix clés."""
    return Verif("montants_derives", frozenset(float(e) for e in extra), "aucun montant inventé (montants dérivés de la grille acceptés)", poids)


def montants_devis(*extra: float, poids: float = 1.0) -> Verif:
    """Alias de montants_ok (même règle : les montants dérivés de la grille sont acceptés)."""
    return montants_ok(*extra, poids=poids)


def nombre(valeur: float, poids: float = 1.0) -> Verif:
    """Ce nombre apparaît dans la réponse, quelle que soit la façon de l'écrire (4 050 / 4050 / 4.050 / 4 050,0, espaces insécables compris)."""
    return Verif("nombre", float(valeur), f"contient le nombre {valeur:g}", poids)


def pourcent(valeur: float, poids: float = 1.0) -> Verif:
    """Ce pourcentage apparaît (1 % / 1,0 % / 1.00% / 1 pour cent)."""
    return Verif("pourcent", float(valeur), f"contient {valeur:g} %", poids)


def meta(genre: str, poids: float = 1.0) -> Verif:
    """« title » (≤ 60 caractères) ou « description » (≤ 155) : la meta est présente, sa longueur RÉELLE respecte la limite, et si la réponse annonce un nombre de caractères,
    il est exact (à 3 près). Ne pas annoncer de nombre est accepté ; ne pas écrire « 155 » aussi."""
    limite = {"title": 60, "description": 155}[genre]
    return Verif("meta", (genre, limite), f"meta {genre} présente, ≤ {limite} caractères réels, nombre annoncé exact", poids)


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
def analyser_meta(reponse: str, genre: str) -> tuple[str, int | None] | None:
    """(texte de la meta, nombre de caractères annoncé ou None), ou None si la meta est introuvable. Formes lues : « Meta title (52/60 car.) : texte »,
    « **Meta description** : texte (147 caractères) », « - Meta description : « texte » », texte sur la ligne suivante."""
    lignes = [re.sub(r"[*`_#]", "", l).strip() for l in (reponse or "").splitlines()]
    for i, l in enumerate(lignes):
        if not re.search(rf"meta[ -]?{genre}", sans_accents(l)):
            continue
        s = re.sub(r"^\W*(?:\d+[.)]\s*)?", "", l)
        annonce = None
        m = re.search(r"\(?[^()]*?(\d{1,3})\s*(?:/\s*\d{1,3}\s*)?(?:car\b|caract|signes)[^()]*\)?", s, re.I)
        if m:
            annonce = int(m.group(1))
            s = s[:m.start()] + s[m.end():]
        s = re.sub(rf"(?i)meta[ -]?{genre}\s*", "", s, count=1)
        s = s.strip(" \t:：-–—|=")
        s = s.strip("«»\"'“”‘’ ")
        if not s and i + 1 < len(lignes):                          # le texte est sur la ligne suivante
            s = lignes[i + 1].strip("«»\"'“”‘’ -–—:")
            m2 = re.search(r"\(?[^()]*?(\d{1,3})\s*(?:/\s*\d{1,3}\s*)?(?:car\b|caract|signes)[^()]*\)?\s*$", s, re.I)
            if m2:
                annonce = annonce or int(m2.group(1))
                s = s[:m2.start()].strip(" \t:-–—|«»\"'“”")
        if s:
            return s, annonce
    return None


STRUCTURES = {
    "liste": re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S", re.M),
    "etapes": re.compile(r"^\s*(?:\d+[.)]|etape \d+|\*\*\d+)", re.M | re.I),
    "tableau": re.compile(r"^\s*\|.+\|\s*$", re.M),
    "yaml": re.compile(r"```ya?ml[\s\S]+?(?:id:|- id:)[\s\S]+?```", re.I),
    "objet": re.compile(r"^\W*objet\s*:", re.M | re.I),
}


def evaluer(v: Verif, reponse: str) -> bool:
    reponse = nettoyer(reponse)
    t = sans_accents(reponse)
    if v.genre == "contient":
        return re.search(sans_accents(v.argument), t) is not None
    if v.genre == "un_de":
        return any(re.search(sans_accents(r), t) for r in v.argument)
    if v.genre == "absent":
        return re.search(sans_accents(v.argument), t) is None
    if v.genre == "feu":
        couleurs = set(re.findall(r"\bfeu\s*[:：]\s*[\W_]{0,8}(orange|rouge|vert)\b", t))       # n'importe où dans une ligne, après du texte, entre parenthèses, en gras…
        if v.argument == "ORANGE":
            return "orange" in couleurs and "rouge" not in couleurs
        if v.argument == "ROUGE":
            return "rouge" in couleurs
        return not (couleurs & {"orange", "rouge"})
    if v.genre == "feu_en_tete":
        premiere = next((l for l in t.splitlines() if re.search(r"\w", l)), "")
        return re.match(r"^\W{0,8}feu\s*[:：]\s*\W{0,8}" + v.argument.lower() + r"\b", premiere) is not None
    if v.genre == "avant":
        rx_avant, rx_apres = v.argument
        commande = re.search(r"^[ \t>`$#-]*(?:sudo\s+)?(?:" + sans_accents(rx_apres) + r")", t, re.M)
        sauvegarde = re.search(sans_accents(rx_avant), t)
        if sauvegarde is None:
            return False
        return commande is None or sauvegarde.start() < commande.start()
    if v.genre == "renvoie":
        return any(sans_accents(n) in t for n in v.argument)
    if v.genre == "prix":
        trouves = montants(reponse)
        return all(any(abs(e - m) < TOLERANCE for m in trouves) for e in v.argument)
    if v.genre == "montants_ok":
        permis = TOUS_LES_PRIX | set(v.argument)
        return all(any(abs(m - p) < TOLERANCE for p in permis) for m in montants(reponse))
    if v.genre == "montants_derives":
        permis = TOUS_LES_PRIX | set(v.argument) | set(montants_derives())
        return all(any(abs(m - p) < TOLERANCE for p in permis) for m in montants(reponse))
    if v.genre == "nombre":
        return any(abs(x - v.argument) < 0.005 for x in nombres(reponse))
    if v.genre == "pourcent":
        return any(abs(x - v.argument) < 0.005 for x in nombres(reponse, pourcent=True))
    if v.genre == "meta":
        genre, limite = v.argument
        trouve = analyser_meta(reponse, genre)
        if trouve is None:
            return False
        texte, annonce = trouve
        return len(texte) <= limite and (annonce is None or abs(annonce - len(texte)) <= 3)
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
