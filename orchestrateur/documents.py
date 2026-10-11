"""Outil « documents » : lecture SEULE d'un NAS monté (SMB…), pour l'Archiviste et les autres agents.

Garanties (toutes vérifiées par des tests) :
  * LECTURE SEULE : ce module n'écrit, ne déplace, ne renomme et ne supprime JAMAIS rien dans le NAS.
    Le seul fichier qu'il écrit est son propre index SQLite (volume dédié). Les fichiers du NAS sont ouverts
    en lecture avec O_RDONLY | O_NOFOLLOW.
  * CONFINEMENT : chemins absolus, « .. », octets nuls et liens symboliques sont refusés ; les liens
    symboliques sont ignorés (invisibles) à l'indexation comme à la lecture.
  * DOSSIERS EXCLUS (RH, paie, pièces d'identité…) : invisibles (jamais indexés, jamais lus, jamais cités).
  * BORNES : taille de fichier, nombre de fichiers parcourus, caractères renvoyés, délai par demande.
  * Seul un EXTRAIT utile part vers le modèle, jamais un document entier.
  * JOURNAL : qui, quand, quel fichier ; jamais le contenu.
Dépendances : bibliothèque standard + pypdf (import paresseux ; sans pypdf, les PDF ne sont indexés que par leur nom).
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import sqlite3
import stat
import threading
import time
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

LOG = logging.getLogger("pk.documents")
logging.getLogger("pypdf").setLevel(logging.ERROR)     # pypdf bavarde sur les PDF abîmés : on journalise nous-mêmes

EXCLUS_PAR_DEFAUT = ("rh", "ressources humaines", "paie", "bulletins de paie", "salaires", "pieces d'identite",
                     "piece d'identite", "identite", "medical", "arret maladie", "contrats de travail")
TYPES = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".txt": "txt", ".csv": "csv", ".md": "txt", ".log": "txt"}
OUTILS_INTERDITS = {"ecrire_document", "supprimer_document", "deplacer_document", "renommer_document",
                    "modifier_document", "creer_document", "copier_document", "ecrire", "supprimer", "deplacer",
                    "renommer", "write_file", "delete_file", "move_file", "rename_file"}
XML_MAX = 60 * 1024 * 1024        # taille décompressée maximale d'un fichier XML lu dans un docx/xlsx (anti « zip bomb »)


class AccesRefuse(Exception):
    """Chemin interdit (hors NAS, lien symbolique, dossier exclu, octet nul…). Message sans détail utile à un attaquant."""


class NasIndisponible(Exception):
    """Le partage n'est pas monté, est vide ou illisible."""


def normaliser(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", str(texte).lower())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texte.replace("’", "'")).strip()


def _aplat(texte: str) -> str:
    """Même texte sans accents ni majuscules, MÊME LONGUEUR (pour retrouver la position d'un mot dans l'original)."""
    sortie = []
    for c in texte:
        base = "".join(x for x in unicodedata.normalize("NFKD", c) if not unicodedata.combining(x))
        sortie.append(base.lower() if len(base) == 1 else (c.lower() if len(c.lower()) == 1 else "?"))
    return "".join(sortie).replace("’", "'")


SYNONYMES_TYPES = {"pdf": "pdf", "docx": "docx", "doc": "docx", "word": "docx", "xlsx": "xlsx", "xls": "xlsx", "excel": "xlsx",
                   "tableur": "xlsx", "txt": "txt", "texte": "txt", "text": "txt", "csv": "csv"}


def normaliser_types(type_: Any) -> tuple[list[str] | None, list[str]]:
    """(types reconnus ou None, mots non reconnus). « Word », ".PDF", « pdf, docx »… acceptés ; le reste est ignoré."""
    if not type_ or not isinstance(type_, (str, list, tuple)):
        return None, []
    brut = type_ if isinstance(type_, str) else ",".join(str(x) for x in type_)
    connus: list[str] = []
    inconnus: list[str] = []
    for mot in re.split(r"[,;/\s]+", normaliser(brut)):
        mot = mot.strip(". ")
        if not mot:
            continue
        t = SYNONYMES_TYPES.get(mot)
        if t:
            if t not in connus:
                connus.append(t)
        else:
            inconnus.append(mot)
    return (connus or None), inconnus


# --- appels d'outils écrits en TEXTE par certains modèles (Qwen, Hermes…) au lieu d'un vrai tool_call ------------
_FUITE = re.compile(r"</?\s*(?:tool_call|function_call|tool_response)\b[^>]*>?|<\s*function=|</\s*function\s*>|<\s*parameter=|</\s*parameter\s*>", re.I)
_BLOC_JSON = re.compile(r"<\s*tool_call\s*>\s*([\[{].*?)(?:<\s*/\s*tool_call\s*>|\Z)", re.S | re.I)
_FONCTION = re.compile(r"<\s*function=\s*([A-Za-z0-9_.\-]+)\s*>(.*?)(?=<\s*/\s*function\s*>|<\s*function=|<\s*/\s*tool_call\s*>|\Z)", re.S | re.I)
_PARAMETRE = re.compile(r"<\s*parameter=\s*([A-Za-z0-9_.\-]+)\s*>(.*?)(?=<\s*/\s*parameter\s*>|<\s*parameter=|<\s*/\s*function\s*>|<\s*/\s*tool_call\s*>|\Z)", re.S | re.I)
_ENTIERS = {"limite", "debut"}


def fuite_balises(texte: Any) -> bool:
    """Vrai si le texte contient des balises d'appel d'outil (<tool_call>, <function=…>, <parameter=…>) : elles ne doivent jamais atteindre l'utilisateur."""
    return isinstance(texte, str) and bool(_FUITE.search(texte))


def nettoyer_balises(texte: Any) -> str:
    """Retire toute balise d'appel d'outil (et le contenu d'un appel complet) ; un texte normal est renvoyé tel quel."""
    if not isinstance(texte, str):
        return ""
    if not fuite_balises(texte):
        return texte
    t = re.sub(r"<\s*tool_call\s*>.*?<\s*/\s*tool_call\s*>", " ", texte, flags=re.S | re.I)
    t = re.sub(r"<\s*tool_call\s*>\s*(?=[\[{]|<\s*function=).*\Z", " ", t, flags=re.S | re.I)          # appel coupé net : tout le reste
    t = re.sub(r"<\s*function=[^>]*>.*?<\s*/\s*function\s*>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<\s*/?\s*(?:tool_call|function_call|tool_response)\b[^>\s]*>?|<\s*function=[^>\s<]*>?|<\s*/\s*function\s*>|"
               r"<\s*parameter=[^>\s<]*>?|<\s*/\s*parameter\s*>", " ", t, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", t)).strip()


def _args_json(valeur: Any) -> dict:
    if isinstance(valeur, dict):
        return valeur
    if isinstance(valeur, str):
        try:
            objet = json.loads(valeur)
            return objet if isinstance(objet, dict) else {}
        except ValueError:
            return {}
    return {}


def _elements_json(corps: str) -> list:
    corps = corps.strip()
    try:
        objet = json.loads(corps)
    except ValueError:
        try:
            objet, _ = json.JSONDecoder().raw_decode(corps)
        except ValueError:
            return []
    return objet if isinstance(objet, list) else [objet]


def appels_en_texte(texte: Any) -> list[dict]:
    """Appels d'outils écrits en texte, au format de vrais tool_calls OpenAI ([] si rien d'exploitable).
    Formats : Qwen « <tool_call><function=nom><parameter=clé>valeur</parameter></function></tool_call> »
    et JSON « <tool_call>{"name": …, "arguments": {…}}</tool_call> » (Hermes)."""
    if not fuite_balises(texte):
        return []
    trouves: list[tuple[int, str, dict]] = []
    for m in _BLOC_JSON.finditer(texte):
        for el in _elements_json(m.group(1)):
            if not isinstance(el, dict):
                continue
            fn = el.get("function")
            nom = el.get("name") or (fn if isinstance(fn, str) else (fn or {}).get("name") if isinstance(fn, dict) else None)
            args = _args_json(el.get("arguments", el.get("parameters", el.get("args", (fn or {}).get("arguments") if isinstance(fn, dict) else {}))))
            if isinstance(nom, str) and nom.strip():
                trouves.append((m.start(), nom.strip(), args))
    zones_json = [(m.start(), m.end()) for m in _BLOC_JSON.finditer(texte)]
    for m in _FONCTION.finditer(texte):
        if any(a <= m.start() < b for a, b in zones_json):
            continue
        args = {}
        for p in _PARAMETRE.finditer(m.group(2)):
            valeur = p.group(2).strip()
            args[p.group(1)] = int(valeur) if p.group(1) in _ENTIERS and re.fullmatch(r"-?\d+", valeur) else valeur
        trouves.append((m.start(), m.group(1), args))
    trouves.sort(key=lambda x: x[0])
    return [{"id": f"call_texte_{i}", "type": "function",
             "function": {"name": nom, "arguments": json.dumps(args, ensure_ascii=False)}} for i, (_, nom, args) in enumerate(trouves, 1)]


def _jetons(texte: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normaliser(texte))


def _entier(env: dict, nom: str, defaut: float, mini: float, maxi: float, entier: bool = True):
    try:
        v = float(env.get(nom, defaut))
    except (TypeError, ValueError):
        LOG.warning("%s illisible, valeur par défaut %s utilisée", nom, defaut)
        v = defaut
    v = max(mini, min(maxi, v))
    return int(v) if entier else v


@dataclass
class Config:
    racine: str = ""
    index: str = ""
    exclus: tuple = EXCLUS_PAR_DEFAUT
    taille_max: int = 15 * 1024 * 1024        # octets : au-delà, le fichier est connu par son nom mais jamais lu
    fichiers_max: int = 3000                  # fichiers examinés au plus par demande (recherche sans index)
    caracteres_max: int = 6000                # caractères renvoyés au plus par demande
    delai: float = 20.0                       # secondes au plus par demande
    texte_max: int = 100_000                  # caractères de texte gardés par fichier dans l'index
    pdf_pages_max: int = 120
    index_delai: float = 3600.0               # secondes au plus par passe d'indexation
    pause: float = 0.002                      # pause entre deux fichiers indexés (ménage le processeur)
    nuit: str = "02:30"
    fuseau: str = "Europe/Paris"
    tours_max: int = 4                        # allers-retours outil <-> modèle au plus
    mode: str = "auto"                        # auto | outils | repli

    @classmethod
    def depuis_env(cls, env: dict | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        brut = (env.get("PK_NAS_EXCLUS") or "").strip()
        # vide = liste par défaut (jamais « aucune exclusion » par accident) ; « - » = aucune exclusion, volontairement
        exclus = EXCLUS_PAR_DEFAUT if not brut else (() if brut == "-" else tuple(x.strip() for x in brut.split(",") if x.strip()))
        mode = env.get("PK_NAS_MODE", "auto").strip().lower()
        return cls(
            racine=env.get("PK_NAS_CHEMIN", "").strip(), index=env.get("PK_NAS_INDEX", "/app/data/index.sqlite"),
            exclus=exclus, taille_max=_entier(env, "PK_NAS_TAILLE_MAX_MO", 15, 1, 500) * 1024 * 1024,
            fichiers_max=_entier(env, "PK_NAS_FICHIERS_MAX", 3000, 10, 200_000),
            caracteres_max=_entier(env, "PK_NAS_CARACTERES_MAX", 6000, 500, 50_000),
            delai=_entier(env, "PK_NAS_DELAI", 20, 2, 300, entier=False),
            texte_max=_entier(env, "PK_NAS_TEXTE_MAX", 100_000, 1000, 2_000_000),
            pdf_pages_max=_entier(env, "PK_NAS_PDF_PAGES", 120, 1, 2000),
            index_delai=_entier(env, "PK_NAS_INDEX_DELAI", 3600, 10, 86_400, entier=False),
            nuit=env.get("PK_NAS_NUIT", "02:30").strip(), fuseau=env.get("PK_FUSEAU", "Europe/Paris"),
            tours_max=_entier(env, "PK_NAS_TOURS", 4, 1, 8), mode=mode if mode in ("auto", "outils", "repli") else "auto")


# ----------------------------------------------------------------------------------------------------
#  Extraction de texte (lecture seule, bornée)
# ----------------------------------------------------------------------------------------------------
def ouvrir_lecture(chemin: str):
    """Ouvre un fichier du NAS en LECTURE SEULE, sans jamais suivre un lien symbolique."""
    fd = os.open(chemin, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    return os.fdopen(fd, "rb")


def _decoder(octets: bytes) -> str:
    try:
        return octets.decode("utf-8")
    except UnicodeDecodeError:
        return octets.decode("cp1252", errors="replace")


def extraire_texte_simple(f, cfg: Config) -> str:
    return _decoder(f.read(min(cfg.taille_max, cfg.texte_max * 4)))[:cfg.texte_max]


def _lire_xml_borne(z: zipfile.ZipFile, nom: str):
    info = z.getinfo(nom)
    if info.file_size > XML_MAX:
        raise ValueError("partie XML trop volumineuse")
    return z.open(info)


def extraire_docx(f, cfg: Config, fin: float) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    morceaux: list[str] = []
    total = 0
    with zipfile.ZipFile(f) as z, _lire_xml_borne(z, "word/document.xml") as x:
        for _, el in ET.iterparse(x, events=("end",)):
            if el.tag == ns + "t" and el.text:
                morceaux.append(el.text)
                total += len(el.text)
            elif el.tag in (ns + "p", ns + "br"):
                morceaux.append("\n")
            elif el.tag == ns + "tab":
                morceaux.append(" ")
            el.clear()
            if total >= cfg.texte_max or time.monotonic() > fin:
                break
    return re.sub(r"\n{3,}", "\n\n", "".join(morceaux))[:cfg.texte_max]


def extraire_xlsx(f, cfg: Config, fin: float) -> str:
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    sortie: list[str] = []
    total = 0
    with zipfile.ZipFile(f) as z:
        noms = set(z.namelist())
        partages: list[str] = []
        if "xl/sharedStrings.xml" in noms:
            with _lire_xml_borne(z, "xl/sharedStrings.xml") as x:
                courant: list[str] = []
                taille = 0
                for _, el in ET.iterparse(x, events=("end",)):
                    if el.tag == ns + "t" and el.text:
                        courant.append(el.text)
                    elif el.tag == ns + "si":
                        s = "".join(courant)
                        partages.append(s)
                        taille += len(s)
                        courant = []
                        el.clear()
                        if taille > cfg.texte_max * 20:      # plafond mémoire des textes partagés
                            break
        feuilles: list[tuple[str, str]] = []
        if "xl/workbook.xml" in noms:
            cibles: dict[str, str] = {}
            if "xl/_rels/workbook.xml.rels" in noms:
                with _lire_xml_borne(z, "xl/_rels/workbook.xml.rels") as x:
                    for _, el in ET.iterparse(x, events=("end",)):
                        if el.tag.endswith("Relationship"):
                            cibles[el.get("Id", "")] = el.get("Target", "")
            with _lire_xml_borne(z, "xl/workbook.xml") as x:
                for _, el in ET.iterparse(x, events=("end",)):
                    if el.tag == ns + "sheet":
                        cible = cibles.get(el.get(rel_ns + "id", ""), "")
                        cible = cible.lstrip("/")
                        chemin = cible if cible.startswith("xl/") else "xl/" + cible
                        if chemin in noms:
                            feuilles.append((el.get("name", "Feuille"), chemin))
        if not feuilles:
            feuilles = [(p.rsplit("/", 1)[-1], p) for p in sorted(noms) if re.match(r"xl/worksheets/sheet\d+\.xml$", p)]
        for nom_feuille, chemin in feuilles:
            sortie.append(f"## {nom_feuille}\n")
            ligne: list[str] = []
            with _lire_xml_borne(z, chemin) as x:
                type_cellule, valeur, texte_inline = "", None, []
                for evenement, el in ET.iterparse(x, events=("start", "end")):
                    if evenement == "start":
                        if el.tag == ns + "c":
                            type_cellule, valeur, texte_inline = el.get("t", ""), None, []
                        continue
                    if el.tag == ns + "v":
                        valeur = el.text
                    elif el.tag == ns + "t" and el.text:
                        texte_inline.append(el.text)
                    elif el.tag == ns + "c":
                        if type_cellule == "s" and valeur is not None and valeur.isdigit() and int(valeur) < len(partages):
                            cellule = partages[int(valeur)]
                        elif type_cellule == "inlineStr":
                            cellule = "".join(texte_inline)
                        else:
                            cellule = valeur or ""
                        if cellule != "":
                            ligne.append(cellule)
                        el.clear()
                    elif el.tag == ns + "row":
                        if ligne:
                            texte = " | ".join(ligne) + "\n"
                            sortie.append(texte)
                            total += len(texte)
                        ligne = []
                        el.clear()
                        if total >= cfg.texte_max or time.monotonic() > fin:
                            break
            if total >= cfg.texte_max or time.monotonic() > fin:
                break
    return "".join(sortie)[:cfg.texte_max]


def extraire_pdf(f, cfg: Config, fin: float) -> tuple[str, str]:
    """Texte intégré seulement (aucun OCR). Renvoie (texte, statut)."""
    try:
        from pypdf import PdfReader  # import paresseux : la dépendance n'est requise que si on lit des PDF
    except ImportError:
        return "", "pdf_indisponible"
    lecteur = PdfReader(f, strict=False)
    if lecteur.is_encrypted:
        try:
            if not lecteur.decrypt(""):
                return "", "chiffre"
        except Exception:  # noqa: BLE001 — chiffrement non pris en charge
            return "", "chiffre"
    morceaux: list[str] = []
    total = 0
    for i, page in enumerate(lecteur.pages):
        if i >= cfg.pdf_pages_max or total >= cfg.texte_max or time.monotonic() > fin:
            break
        texte = page.extract_text() or ""
        morceaux.append(texte)
        total += len(texte)
    texte = "\n".join(morceaux).strip()[:cfg.texte_max]
    return texte, ("ok" if texte else "sans_texte")


def extraire(chemin: str, ext: str, taille: int, cfg: Config, fin: float) -> tuple[str, str]:
    """(texte, statut) d'un fichier du NAS. Ne lève jamais d'exception."""
    type_ = TYPES.get(ext)
    if type_ is None:
        return "", "nom_seulement"
    if taille > cfg.taille_max:
        return "", "trop_gros"
    try:
        with ouvrir_lecture(chemin) as f:
            if type_ == "pdf":
                return extraire_pdf(f, cfg, fin)
            if type_ == "docx":
                texte = extraire_docx(f, cfg, fin)
            elif type_ == "xlsx":
                texte = extraire_xlsx(f, cfg, fin)
            else:
                texte = extraire_texte_simple(f, cfg)
        return texte, ("ok" if texte.strip() else "sans_texte")
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError, ValueError) as e:
        LOG.warning("lecture impossible (%s) : %s", type(e).__name__, os.path.basename(chemin))
        return "", "illisible"
    except Exception as e:  # noqa: BLE001 — un fichier abîmé ne doit jamais arrêter l'indexation
        LOG.warning("extraction en échec (%s) : %s", type(e).__name__, os.path.basename(chemin))
        return "", "illisible"


# ----------------------------------------------------------------------------------------------------
#  Le service
# ----------------------------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS fichiers (
    id INTEGER PRIMARY KEY, chemin TEXT UNIQUE NOT NULL, nom TEXT NOT NULL, nom_norm TEXT NOT NULL,
    dossier TEXT NOT NULL, dossier_norm TEXT NOT NULL, ext TEXT NOT NULL, type TEXT NOT NULL,
    taille INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, modifie TEXT NOT NULL, indexe_le TEXT NOT NULL,
    statut TEXT NOT NULL, texte TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_fichiers_nom ON fichiers(nom_norm);
CREATE INDEX IF NOT EXISTS idx_fichiers_type ON fichiers(type);
CREATE INDEX IF NOT EXISTS idx_fichiers_modifie ON fichiers(modifie);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, demandeur TEXT NOT NULL, agent TEXT NOT NULL,
    action TEXT NOT NULL, cible TEXT NOT NULL, nb INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS meta (cle TEXT PRIMARY KEY, valeur TEXT);
"""


class Documents:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tz = _fuseau(cfg.fuseau)
        self.verrou = threading.Lock()
        self.fts = False
        racine = cfg.racine
        if not racine or not os.path.isabs(racine):
            raise ValueError("PK_NAS_CHEMIN doit être un chemin absolu")
        self.racine = os.path.realpath(racine)
        self._motifs = [normaliser(m) for m in cfg.exclus if normaliser(m)]
        with self._base() as con:
            con.executescript(SCHEMA)
            try:
                con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(nom, dossier, texte, "
                            "tokenize='unicode61 remove_diacritics 2')")
                self.fts = True
            except sqlite3.OperationalError:
                LOG.warning("SQLite sans FTS5 : recherche dans le texte plus lente (LIKE)")

    # ------------------------------------------------------------------ base
    @contextmanager
    def _base(self):
        con = sqlite3.connect(self.cfg.index, timeout=10)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            yield con
            con.commit()
        finally:
            con.close()

    def _meta(self, con: sqlite3.Connection, cle: str, valeur: str | None = None):
        if valeur is None:
            r = con.execute("SELECT valeur FROM meta WHERE cle=?", (cle,)).fetchone()
            return r["valeur"] if r else None
        con.execute("INSERT INTO meta(cle,valeur) VALUES(?,?) ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
                    (cle, valeur))

    # ------------------------------------------------------------------ exclusions et confinement
    def exclu(self, chemin_relatif: str) -> bool:
        """Vrai si le chemin (relatif au NAS) passe par un dossier ou est un fichier exclu. Insensible aux accents/majuscules."""
        for composant in PurePosixPath(chemin_relatif).parts:
            n = normaliser(composant)
            jetons = _jetons(composant)
            for motif in self._motifs:
                if any(c in motif for c in "*?["):
                    if fnmatch.fnmatchcase(n, motif):
                        return True
                    continue
                mj = _jetons(motif)
                if n == motif or (mj and any(jetons[i:i + len(mj)] == mj for i in range(len(jetons) - len(mj) + 1))):
                    return True
        return False

    def resoudre(self, chemin_relatif: str) -> tuple[str, str]:
        """(chemin absolu, chemin relatif normalisé) d'un fichier du NAS, ou AccesRefuse."""
        if not isinstance(chemin_relatif, str) or not chemin_relatif.strip():
            raise AccesRefuse("chemin vide")
        brut = chemin_relatif.strip()
        if "\x00" in brut or "\\" in brut or brut.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", brut):
            raise AccesRefuse("chemin non autorisé")
        parties = [p for p in brut.split("/") if p not in ("", ".")]
        if not parties or any(p == ".." for p in parties):
            raise AccesRefuse("chemin non autorisé")
        relatif = "/".join(parties)
        if self.exclu(relatif):
            raise AccesRefuse("document introuvable")          # même message qu'un fichier absent : l'exclu est invisible
        courant = self.racine
        for p in parties:
            courant = os.path.join(courant, p)
            try:
                st = os.lstat(courant)
            except FileNotFoundError:
                raise AccesRefuse("document introuvable") from None
            except OSError:
                raise AccesRefuse("document illisible") from None
            if stat.S_ISLNK(st.st_mode):
                raise AccesRefuse("document introuvable")      # liens symboliques : invisibles
        reel = os.path.realpath(courant)
        if reel != courant and os.path.commonpath([reel, self.racine]) != self.racine:
            raise AccesRefuse("chemin non autorisé")
        if os.path.commonpath([courant, self.racine]) != self.racine or not stat.S_ISREG(st.st_mode):
            raise AccesRefuse("document introuvable")
        return courant, relatif

    # ------------------------------------------------------------------ indexation
    def _verifier_nas(self) -> None:
        try:
            with os.scandir(self.racine) as it:
                if next(it, None) is None:
                    raise NasIndisponible("le dossier du NAS est vide (partage non monté ?)")
        except OSError as e:
            raise NasIndisponible(f"NAS illisible ({type(e).__name__})") from None

    def _parcourir(self) -> Iterator[tuple[str, str, os.stat_result]]:
        """Fichiers réguliers du NAS (hors exclusions et liens symboliques) : (chemin absolu, relatif, stat)."""
        pile = [""]
        while pile:
            rel_dossier = pile.pop()
            try:
                with os.scandir(os.path.join(self.racine, rel_dossier) if rel_dossier else self.racine) as it:
                    entrees = sorted(it, key=lambda e: e.name)
            except OSError as e:
                LOG.warning("dossier illisible (%s) : %s", type(e).__name__, rel_dossier or "/")
                continue
            for e in entrees:
                rel = f"{rel_dossier}/{e.name}" if rel_dossier else e.name
                try:
                    if e.is_symlink() or self.exclu(rel):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        pile.append(rel)
                    elif e.is_file(follow_symlinks=False):
                        yield e.path, rel, e.stat(follow_symlinks=False)
                except OSError:
                    continue

    def indexer(self, delai: float | None = None) -> dict[str, Any]:
        """Met l'index à jour (reprise : seuls les fichiers nouveaux ou modifiés sont relus). Une seule passe à la fois."""
        if not self.verrou.acquire(blocking=False):
            return {"deja_en_cours": True}
        debut = time.monotonic()
        fin = debut + (delai if delai is not None else self.cfg.index_delai)
        bilan = {"vus": 0, "indexes": 0, "inchanges": 0, "echecs": 0, "purges": 0, "complet": False, "message": ""}
        try:
            self._verifier_nas()
            with self._base() as con:
                connus = {r["chemin"]: (r["taille"], r["mtime_ns"]) for r in con.execute(
                    "SELECT chemin, taille, mtime_ns FROM fichiers")}
                vus: set[str] = set()
                en_attente = 0
                interrompu = False
                for absolu, rel, st in self._parcourir():
                    vus.add(rel)
                    bilan["vus"] += 1
                    if connus.get(rel) == (st.st_size, st.st_mtime_ns):
                        bilan["inchanges"] += 1
                        continue
                    if time.monotonic() > fin:
                        interrompu = True
                        break
                    nom = os.path.basename(rel)
                    ext = os.path.splitext(nom)[1].lower()
                    texte, statut = extraire(absolu, ext, st.st_size, self.cfg, fin)
                    if statut == "illisible":
                        bilan["echecs"] += 1
                    self._enregistrer(con, rel, nom, ext, st, texte, statut)
                    bilan["indexes"] += 1
                    en_attente += 1
                    if en_attente >= 50:
                        con.commit()
                        en_attente = 0
                    if self.cfg.pause:
                        time.sleep(self.cfg.pause)
                con.commit()
                bilan["complet"] = not interrompu
                if bilan["complet"]:
                    bilan["purges"] = self._purger(con, vus, bilan)
                self._meta(con, "derniere_passe", datetime.now(self.tz).isoformat(timespec="seconds"))
                self._meta(con, "derniere_passe_complete", "1" if bilan["complet"] else "0")
                con.commit()
        except NasIndisponible as e:
            bilan["message"] = f"NAS indisponible : {e} — index conservé tel quel"
            LOG.warning(bilan["message"])
        finally:
            bilan["duree_s"] = round(time.monotonic() - debut, 2)
            self.verrou.release()
        return bilan

    def _enregistrer(self, con, rel: str, nom: str, ext: str, st: os.stat_result, texte: str, statut: str) -> None:
        dossier = str(PurePosixPath(rel).parent)
        dossier = "" if dossier == "." else dossier
        valeurs = (rel, nom, normaliser(nom), dossier, normaliser(dossier), ext, TYPES.get(ext, "autre"), st.st_size,
                   st.st_mtime_ns, datetime.fromtimestamp(st.st_mtime, self.tz).strftime("%Y-%m-%d"),
                   datetime.now(self.tz).isoformat(timespec="seconds"), statut, texte)
        ancien = con.execute("SELECT id FROM fichiers WHERE chemin=?", (rel,)).fetchone()
        if ancien:
            con.execute("""UPDATE fichiers SET nom=?, nom_norm=?, dossier=?, dossier_norm=?, ext=?, type=?,
                           taille=?, mtime_ns=?, modifie=?, indexe_le=?, statut=?, texte=? WHERE id=?""",
                        (*valeurs[1:], ancien["id"]))
            rowid = ancien["id"]
            if self.fts:
                con.execute("DELETE FROM fts WHERE rowid=?", (rowid,))
        else:
            rowid = con.execute("""INSERT INTO fichiers(chemin, nom, nom_norm, dossier, dossier_norm, ext, type, taille,
                                   mtime_ns, modifie, indexe_le, statut, texte) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                valeurs).lastrowid
        if self.fts:
            con.execute("INSERT INTO fts(rowid, nom, dossier, texte) VALUES(?,?,?,?)", (rowid, nom, dossier, texte))

    def _purger(self, con, vus: set[str], bilan: dict) -> int:
        """Retire de l'index ce qui a disparu du NAS (ou est devenu exclu) — jamais sur un parcours incomplet,
        et jamais de façon massive (un partage à moitié monté ne doit pas vider l'index)."""
        total = con.execute("SELECT COUNT(*) FROM fichiers").fetchone()[0]
        a_retirer = [r["id"] for r in con.execute("SELECT id, chemin FROM fichiers") if r["chemin"] not in vus]
        if not a_retirer:
            return 0
        if total >= 20 and len(a_retirer) > total // 2:
            bilan["message"] = (f"suppression massive refusée ({len(a_retirer)} sur {total}) : "
                                "partage peut-être partiellement monté, index conservé")
            LOG.warning(bilan["message"])
            return 0
        for rowid in a_retirer:
            con.execute("DELETE FROM fichiers WHERE id=?", (rowid,))
            if self.fts:
                con.execute("DELETE FROM fts WHERE rowid=?", (rowid,))
        return len(a_retirer)

    def etat(self) -> dict[str, Any]:
        with self._base() as con:
            n = con.execute("SELECT COUNT(*) FROM fichiers").fetchone()[0]
            par_statut = {r["statut"]: r["n"] for r in con.execute("SELECT statut, COUNT(*) n FROM fichiers GROUP BY statut")}
            return {"actif": True, "fichiers_indexes": n, "par_statut": par_statut, "fts5": self.fts,
                    "derniere_passe": self._meta(con, "derniere_passe"),
                    "derniere_passe_complete": self._meta(con, "derniere_passe_complete") == "1",
                    "en_cours": self.verrou.locked()}

    # ------------------------------------------------------------------ journal (jamais de contenu)
    def journaliser(self, demandeur: str, agent: str, action: str, cible: str, nb: int = 0, ok: bool = True) -> None:
        try:
            with self._base() as con:
                con.execute("INSERT INTO journal(ts, demandeur, agent, action, cible, nb, ok) VALUES(?,?,?,?,?,?,?)",
                            (datetime.now(self.tz).isoformat(timespec="seconds"), str(demandeur)[:120], str(agent)[:60],
                             action, str(cible)[:1000], nb, 1 if ok else 0))
        except sqlite3.Error as e:
            LOG.warning("journal des consultations : écriture impossible (%s)", type(e).__name__)

    # ------------------------------------------------------------------ recherche
    def _fenetre(self, texte: str, jetons: list[str], largeur: int = 160) -> str:
        """Court extrait autour du premier terme trouvé (sinon le début du texte)."""
        if not texte:
            return ""
        norm = _aplat(texte)
        pos = -1
        for j in jetons:
            pos = norm.find(j)
            if pos != -1:
                break
        debut = max(0, pos - largeur // 2) if pos != -1 else 0
        extrait = re.sub(r"\s+", " ", texte[debut:debut + largeur * 2]).strip()
        return ("… " if debut > 0 else "") + extrait

    def rechercher(self, texte: str | None = None, nom: str | None = None, dossier: str | None = None,
                   type: str | None = None, apres: str | None = None, avant: str | None = None,
                   limite: int | None = None) -> dict[str, Any]:
        """Recherche. Un type demandé qui ne correspond à rien (« pdf,docx » pour un .txt) ne fait JAMAIS échouer la recherche :
        on réessaie sans filtre de type et on le dit."""
        types, inconnus = normaliser_types(type)
        res = self._rechercher(texte, nom, dossier, types, apres, avant, limite)
        if not res["trouve"] and types:
            sans = self._rechercher(texte, nom, dossier, None, apres, avant, limite)
            if sans["trouve"]:
                sans["message"] = (f"Aucun document du type demandé ({type}) : voici les résultats sans filtre de type. "
                                   "Le type réel de chaque document est indiqué.")
                return sans
        if inconnus and res["trouve"]:
            res["message"] = (res.get("message", "") + f" Type non reconnu ignoré : {', '.join(inconnus)}.").strip()
        return res

    def _rechercher(self, texte, nom, dossier, types, apres, avant, limite) -> dict[str, Any]:
        fin = time.monotonic() + self.cfg.delai
        limite = max(1, min(int(limite or 8), 20))
        where, params = [], []
        jetons = _jetons(texte or "")[:8]
        for j in _jetons(nom or ""):
            where.append("f.nom_norm LIKE ?")
            params.append(f"%{j}%")
        for j in _jetons(dossier or ""):
            where.append("f.dossier_norm LIKE ?")
            params.append(f"%{j}%")
        if types:
            where.append("f.type IN (%s)" % ",".join("?" * len(types)))
            params += types
        for borne, op in ((apres, ">="), (avant, "<=")):
            if borne:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(borne)):
                    return {"trouve": False, "resultats": [], "message": "Date invalide : format AAAA-MM-JJ attendu."}
                where.append(f"f.modifie {op} ?")
                params.append(str(borne))
        if not (jetons or where):
            return {"trouve": False, "resultats": [], "message": "Aucun critère de recherche fourni (texte, nom, dossier, type, dates)."}
        with self._base() as con:
            total_index = con.execute("SELECT COUNT(*) FROM fichiers").fetchone()[0]
            if total_index == 0:
                return self._recherche_sans_index(nom or texte or "", fin)
            lignes, approx = self._requete(con, jetons, where, params, limite, et=True)
            if not lignes and len(jetons) > 1:
                # correspondance partielle : au moins la moitié des mots demandés doivent figurer dans le document
                minimum = (len(jetons) + 1) // 2
                lignes, approx = self._requete(con, jetons, where, params, limite, et=False)
                lignes = [r for r in lignes if sum(1 for j in jetons if j in _aplat(f"{r['chemin']} {r['texte']}")) >= minimum]
            budget = self.cfg.caracteres_max
            resultats, utilises, tronque = [], 0, False
            for r in lignes:
                if len(resultats) >= limite:
                    tronque = True                              # il y avait d'autres documents correspondants
                    break
                if time.monotonic() > fin:
                    tronque = True
                    break
                if self.exclu(r["chemin"]):
                    continue                                   # défense en profondeur : un exclu ne sort jamais
                extrait = self._fenetre(r["texte"], jetons)[:300]
                item = {"chemin": r["chemin"], "nom": r["nom"], "type": r["type"], "taille_octets": r["taille"],
                        "modifie": r["modifie"], "extrait": extrait}
                if r["statut"] not in ("ok",):
                    item["note"] = {"sans_texte": "pas de texte lisible (scan ou image : pas d'OCR)",
                                    "trop_gros": "fichier trop gros pour être lu", "illisible": "fichier illisible",
                                    "nom_seulement": "format non lu : trouvé par son nom seulement",
                                    "chiffre": "fichier protégé par mot de passe",
                                    "pdf_indisponible": "lecteur PDF indisponible"}.get(r["statut"], r["statut"])
                taille_item = len(json.dumps(item, ensure_ascii=False))
                if utilises + taille_item > budget and resultats:
                    tronque = True
                    break
                resultats.append(item)
                utilises += taille_item
        if not resultats:
            return {"trouve": False, "resultats": [], "message": "Aucun document trouvé pour cette recherche."}
        sortie = {"trouve": True, "resultats": resultats, "nombre": len(resultats), "tronque": tronque}
        if approx:
            sortie["message"] = "Correspondance partielle : tous les mots demandés ne figurent pas dans chaque document."
        return sortie

    def _requete(self, con, jetons, where, params, limite, et: bool):
        rows: list[sqlite3.Row] = []
        champs = "f.chemin, f.nom, f.type, f.taille, f.modifie, f.statut, f.texte"
        plafond = min(self.cfg.fichiers_max, 500)
        if jetons and self.fts:
            op = " AND " if et else " OR "
            requete = op.join(f'"{j}"*' for j in jetons)
            sql = (f"SELECT {champs} FROM fts JOIN fichiers f ON f.id = fts.rowid WHERE fts MATCH ? "
                   + ("AND " + " AND ".join(where) if where else "") + " ORDER BY bm25(fts, 6.0, 2.0, 1.0) LIMIT ?")
            try:
                rows = con.execute(sql, [requete, *params, min(limite * 3, plafond)]).fetchall()
            except sqlite3.OperationalError as e:
                LOG.warning("requête de recherche refusée (%s)", e)
                rows = []
        elif jetons:
            cond = [" f.texte LIKE ? OR f.nom_norm LIKE ? " for _ in jetons]
            lien = " AND " if et else " OR "
            sql = (f"SELECT {champs} FROM fichiers f WHERE (" + lien.join(f"({c})" for c in cond) + ")"
                   + ("".join(" AND " + w for w in where)) + " LIMIT ?")
            args = [x for j in jetons for x in (f"%{j}%", f"%{j}%")]
            rows = con.execute(sql, [*args, *params, min(limite * 3, plafond)]).fetchall()
        else:
            sql = f"SELECT {champs} FROM fichiers f WHERE " + " AND ".join(where) + " ORDER BY f.modifie DESC LIMIT ?"
            rows = con.execute(sql, [*params, min(limite * 3, plafond)]).fetchall()
        return rows[:limite * 3], (not et)

    def _recherche_sans_index(self, requete: str, fin: float) -> dict[str, Any]:
        """Index encore vide : balayage des NOMS seulement, borné par le nombre de fichiers et le délai."""
        jetons = _jetons(requete)
        trouves, vus = [], 0
        for _, rel, st in self._parcourir():
            vus += 1
            if vus > self.cfg.fichiers_max or time.monotonic() > fin:
                break
            n = normaliser(rel)
            if jetons and all(j in n for j in jetons):
                trouves.append({"chemin": rel, "nom": os.path.basename(rel), "type": TYPES.get(os.path.splitext(rel)[1].lower(), "autre"),
                                "taille_octets": st.st_size, "extrait": "", "note": "index pas encore construit : trouvé par le nom seulement"})
                if len(trouves) >= 8:
                    break
        if not trouves:
            return {"trouve": False, "resultats": [], "message": "Aucun document trouvé (l'index n'est pas encore construit : recherche par nom seulement)."}
        return {"trouve": True, "resultats": trouves, "nombre": len(trouves), "tronque": False,
                "message": "Index pas encore construit : résultats limités aux noms de fichiers."}

    # ------------------------------------------------------------------ lecture d'un extrait
    def lire(self, chemin: str, mots: str | None = None, debut: int = 0) -> dict[str, Any]:
        absolu, relatif = self.resoudre(chemin)
        fin = time.monotonic() + self.cfg.delai
        st = os.lstat(absolu)
        ext = os.path.splitext(relatif)[1].lower()
        texte, statut = extraire(absolu, ext, st.st_size, self.cfg, fin)
        info = {"chemin": relatif, "nom": os.path.basename(relatif), "type": TYPES.get(ext, "autre"),
                "taille_octets": st.st_size, "modifie": datetime.fromtimestamp(st.st_mtime, self.tz).strftime("%Y-%m-%d")}
        if statut != "ok":
            motif = {"sans_texte": "ce document ne contient pas de texte lisible (scan ou image : pas d'OCR)",
                     "trop_gros": "fichier trop gros pour être lu", "illisible": "fichier illisible ou abîmé",
                     "nom_seulement": "format non pris en charge (seuls PDF, Word, Excel, txt, csv sont lus)",
                     "chiffre": "fichier protégé par mot de passe", "pdf_indisponible": "lecteur PDF indisponible"}[statut]
            return {**info, "lisible": False, "motif": motif}
        budget = self.cfg.caracteres_max
        jetons = _jetons(mots or "")
        if jetons:
            passages, deja = [], []
            norm = _aplat(texte)
            for j in jetons:
                for m in re.finditer(re.escape(j), norm):
                    if any(abs(m.start() - p) < 300 for p in deja):
                        continue
                    deja.append(m.start())
                    a = max(0, m.start() - 200)
                    passages.append(re.sub(r"\s+", " ", texte[a:a + 500]).strip())
                    if len(passages) >= 8 or sum(map(len, passages)) > budget:
                        break
            if passages:
                sortie, total = [], 0
                for p in passages:
                    if total + len(p) > budget and sortie:
                        break
                    sortie.append(p)
                    total += len(p)
                return {**info, "lisible": True, "passages": sortie, "caracteres_total": len(texte)}
            return {**info, "lisible": True, "passages": [], "caracteres_total": len(texte),
                    "message": "Aucun passage ne contient ces mots dans ce document."}
        debut = max(0, int(debut or 0))
        extrait = re.sub(r"[ \t]+", " ", texte[debut:debut + budget]).strip()
        sortie = {**info, "lisible": True, "extrait": extrait, "debut": debut, "caracteres_total": len(texte)}
        if debut + budget < len(texte):
            sortie["suite_disponible"] = True
            sortie["debut_suivant"] = debut + budget
        return sortie

    def nom_existe(self, nom: str) -> bool:
        """Vrai si un fichier de ce nom (ou ce chemin) existe dans l'index et n'est pas exclu."""
        with self._base() as con:
            for r in con.execute("SELECT chemin FROM fichiers WHERE nom_norm=? OR chemin=? LIMIT 20",
                                 (normaliser(os.path.basename(nom)), nom)):
                if not self.exclu(r["chemin"]):
                    return True
        return False

    # ------------------------------------------------------------------ point d'entrée des outils (appelé par l'orchestrateur)
    def executer(self, outil: str, arguments: Any, demandeur: str = "inconnu", agent: str = "-") -> str:
        """Exécute un outil demandé par le modèle ; renvoie TOUJOURS une chaîne JSON bornée, jamais d'exception."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except ValueError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            if normaliser(outil).replace(" ", "_") in OUTILS_INTERDITS or outil not in ("rechercher_documents", "lire_document"):
                self.journaliser(demandeur, agent, "refus", f"outil:{outil}", ok=False)
                return self._json({"erreur": "Action refusée : le NAS est en LECTURE SEULE. Seules la recherche et la lecture "
                                             "d'extraits sont possibles ; rien ne peut être écrit, déplacé, renommé ou supprimé.",
                                   "trouve": False})
            if outil == "rechercher_documents":
                res = self.rechercher(**{k: arguments.get(k) for k in ("texte", "nom", "dossier", "type", "apres", "avant", "limite")})
                crit = {k: arguments.get(k) for k in ("texte", "nom", "dossier", "type", "apres", "avant") if arguments.get(k)}
                self.journaliser(demandeur, agent, "recherche",
                                 json.dumps(crit, ensure_ascii=False)[:300] + " -> " + "; ".join(r["chemin"] for r in res["resultats"]),
                                 nb=len(res["resultats"]))
                return self._json(res)
            res = self.lire(str(arguments.get("chemin", "")), arguments.get("mots"), arguments.get("debut") or 0)
            self.journaliser(demandeur, agent, "lecture", res["chemin"], nb=1, ok=bool(res.get("lisible")))
            return self._json(res)
        except AccesRefuse as e:
            self.journaliser(demandeur, agent, "refus", str(arguments.get("chemin", ""))[:300], ok=False)
            return self._json({"erreur": str(e), "trouve": False})
        except (OSError, sqlite3.Error, TypeError, ValueError) as e:
            LOG.warning("outil %s en échec (%s)", outil, type(e).__name__)
            return self._json({"erreur": "Le NAS ou l'index est momentanément indisponible.", "trouve": False})

    def _json(self, objet: dict) -> str:
        texte = json.dumps(objet, ensure_ascii=False)
        limite = self.cfg.caracteres_max + 1500                  # marge pour les clés et les chemins
        if len(texte) > limite:
            objet = {k: v for k, v in objet.items() if k not in ("extrait", "passages", "resultats")}
            objet["tronque"] = True
            objet["erreur"] = "Réponse trop longue, affinez la recherche."
            texte = json.dumps(objet, ensure_ascii=False)[:limite]
        return texte


def _fuseau(nom: str) -> ZoneInfo:
    try:
        return ZoneInfo(nom)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


# ----------------------------------------------------------------------------------------------------
#  Ce que voit le modèle
# ----------------------------------------------------------------------------------------------------
OUTILS_OPENAI = [
    {"type": "function", "function": {
        "name": "rechercher_documents",
        "description": ("Cherche des documents dans le NAS d'ABI Holding (lecture seule) par nom, dossier, date, type ou mots du "
                        "contenu. Renvoie le chemin exact et un court extrait de chaque document trouvé."),
        "parameters": {"type": "object", "properties": {
            "texte": {"type": "string", "description": "Mots à chercher dans le nom, le dossier et le contenu"},
            "nom": {"type": "string", "description": "Mots du nom du fichier"},
            "dossier": {"type": "string", "description": "Mots du chemin du dossier (ex. Oliviers, acquéreurs)"},
            "type": {"type": "string", "description": "pdf, docx, xlsx, txt ou csv (plusieurs séparés par une virgule). Facultatif : à omettre en cas de doute, un type sans correspondance est ignoré"},
            "apres": {"type": "string", "description": "Modifié à partir du (AAAA-MM-JJ)"},
            "avant": {"type": "string", "description": "Modifié jusqu'au (AAAA-MM-JJ)"},
            "limite": {"type": "integer", "description": "Nombre maximal de résultats (8 par défaut, 20 au plus)"}}}}},
    {"type": "function", "function": {
        "name": "lire_document",
        "description": ("Lit un extrait d'un document du NAS à partir de son chemin EXACT (tel que renvoyé par la recherche). "
                        "Donne mots pour obtenir les passages utiles, sinon le début du texte."),
        "parameters": {"type": "object", "properties": {
            "chemin": {"type": "string", "description": "Chemin exact relatif au NAS, tel que renvoyé par rechercher_documents"},
            "mots": {"type": "string", "description": "Mots dont on veut les passages"},
            "debut": {"type": "integer", "description": "Position de départ dans le texte (pour lire la suite)"}},
            "required": ["chemin"]}}},
]

INSTRUCTIONS_AGENT = """## Outil « documents » (NAS d'ABI Holding, LECTURE SEULE)
Tu peux chercher et lire des documents du NAS avec les outils rechercher_documents et lire_document.
Règles :
- Quand on te demande un document, une pièce ou une information qui peut s'y trouver, tu CHERCHES d'abord avec l'outil. Tu ne réponds jamais de mémoire.
- Tu donnes TOUJOURS le chemin exact tel que renvoyé par l'outil, avec un court extrait. Tu n'inventes JAMAIS un document, un nom de fichier, un chemin ou un contenu.
- Si l'outil ne trouve rien, tu le dis clairement (« introuvable dans le NAS ») et tu proposes d'autres mots de recherche.
- Tu ne peux ni écrire, ni déplacer, ni renommer, ni supprimer : tu proposes, la direction décide. Une telle demande est refusée.
- Tu appelles les outils par le mécanisme prévu pour cela ; tu n'écris JAMAIS un appel d'outil en texte dans ta réponse (pas de <tool_call>, pas de <function=…>).
- Certains dossiers (RH, paie, pièces d'identité…) sont invisibles pour toi : ne cherche pas à les atteindre.
- Tu ne reproduis jamais un document en entier : seulement l'extrait utile.
- Les documents scannés sans texte ne sont pas lisibles (pas d'OCR) : dis-le."""

CONTEXTE_REPLI = """## Documents trouvés dans le NAS pour cette demande (extraits, LECTURE SEULE)
{blocs}
Règles : tu réponds UNIQUEMENT d'après ces extraits. Tu cites le chemin exact tel qu'il est écrit ci-dessus. Si rien ne correspond,
dis « introuvable dans le NAS ». Tu n'inventes jamais un document, un nom de fichier ou un contenu."""

CONTEXTE_REPLI_VIDE = """## Recherche dans le NAS pour cette demande
Aucun document trouvé dans le NAS. Tu le dis clairement (« introuvable dans le NAS »), tu n'inventes aucun document et tu proposes
d'autres mots de recherche."""


def contexte_repli(docs: "Documents", demande: str, demandeur: str, agent: str) -> str:
    """Repli sans appel d'outils : recherche dans l'index d'après la demande, extraits injectés dans le contexte."""
    texte = docs.executer("rechercher_documents", {"texte": demande[:300], "limite": 5}, demandeur, agent)
    try:
        res = json.loads(texte)
    except ValueError:
        res = {}
    if not res.get("trouve"):
        return CONTEXTE_REPLI_VIDE
    blocs = "\n".join(f"- {r['chemin']} ({r['type']}, modifié le {r.get('modifie', '?')}) : {r.get('extrait', '') or r.get('note', '')}"
                      for r in res["resultats"])
    return CONTEXTE_REPLI.format(blocs=blocs)


def chemins_cites(reponse: str) -> list[str]:
    """Noms de fichiers (extensions lues) cités dans une réponse du modèle."""
    return [m.strip(".,;:()[]«»\"'") for m in re.findall(r"[^\s|<>\"'`]+\.(?:pdf|docx|xlsx|txt|csv)\b", reponse, re.I)]


def prochaine_execution(maintenant: datetime, heure: str) -> datetime:
    """Prochaine occurrence de HH:MM (fuseau de `maintenant`) strictement après `maintenant`."""
    try:
        h, m = (int(x) for x in heure.split(":"))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
    except ValueError:
        h, m = 2, 30
    cible = maintenant.replace(hour=h, minute=m, second=0, microsecond=0)
    if cible <= maintenant:
        cible += timedelta(days=1)
    return cible
