"""Accès au web pour les agents : recherche (SearXNG privé, repli DuckDuckGo), lecture de pages, audit de site,
recherche d'entreprises (API publique recherche-entreprises.api.gouv.fr).

Sécurité : seules les adresses publiques sont joignables (pas de 127.0.0.1, 10.x, 172.16-31.x, 192.168.x, réseau Docker…),
redirections revérifiées une à une, taille et durée plafonnées."""
import html as htmlmod
import ipaddress
import json
import re
import socket
import time
import urllib.parse
from html.parser import HTMLParser

import httpx

from . import config

UA = "Mozilla/5.0 (compatible; PixelKreol-BureauIA/1.2; +https://pixelkreol.mebeeltech.online)"
TAILLE_MAX = 3 * 1024 * 1024
PORTS_OK = {80, 443, 8080, 8443}
API_ENTREPRISES = "https://recherche-entreprises.api.gouv.fr/search"


class WebErreur(Exception):
    pass


# ------------------------------------------------------------------ récupération sûre
def _ip_publique(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast
                or a.is_unspecified or (a.version == 6 and a.ipv4_mapped and not ipaddress.ip_address(a.ipv4_mapped).is_global))


def _verifier_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    u = urllib.parse.urlsplit(url)
    if u.scheme.lower() not in ("http", "https") or not u.hostname:
        raise WebErreur("Adresse invalide")
    port = u.port or (443 if u.scheme.lower() == "https" else 80)
    if port not in PORTS_OK:
        raise WebErreur(f"Port {port} non autorisé")
    try:
        ips = {a[4][0] for a in socket.getaddrinfo(u.hostname, port, proto=socket.IPPROTO_TCP)}
    except OSError:
        raise WebErreur(f"Nom de domaine introuvable : {u.hostname}")
    if not ips or not all(_ip_publique(ip) for ip in ips):
        raise WebErreur("Adresse interne ou privée : accès refusé")
    return url


def recuperer(url: str, max_redirections: int = 5, timeout: float = 20) -> tuple:
    """Renvoie (url_finale, statut, entetes, texte, duree)."""
    t0 = time.time()
    with httpx.Client(headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5"},
                      timeout=timeout, follow_redirects=False) as c:
        for _ in range(max_redirections + 1):
            url = _verifier_url(url)
            with c.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urllib.parse.urljoin(url, r.headers["location"])
                    continue
                corps = b""
                for bloc in r.iter_bytes():
                    corps += bloc
                    if len(corps) > TAILLE_MAX:
                        break
                enc = r.encoding or "utf-8"
                try:
                    texte = corps.decode(enc, "replace")
                except LookupError:
                    texte = corps.decode("utf-8", "replace")
                return str(r.url), r.status_code, dict(r.headers), texte, time.time() - t0
    raise WebErreur("Trop de redirections")


# ------------------------------------------------------------------ analyse HTML
class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.metas, self.h, self.imgs, self.links, self.text = "", {}, [], [], [], []
        self.jsonld, self.lang, self._skip, self._hbuf, self._in_title = 0, "", 0, None, False
        self.tel, self.forms = [], 0

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang", "")
        if tag in ("script", "style", "noscript", "svg", "template"):
            self._skip += 1
            if tag == "script" and a.get("type") == "application/ld+json":
                self.jsonld += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            k = (a.get("name") or a.get("property") or "").lower()
            if k:
                self.metas[k] = a.get("content", "")
        if tag == "link" and "canonical" in a.get("rel", "").lower():
            self.metas["canonical"] = a.get("href", "")
        if tag in ("h1", "h2", "h3"):
            self._hbuf = [tag, ""]
        if tag == "img":
            self.imgs.append(a)
        if tag == "form":
            self.forms += 1
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
            if a["href"].startswith(("tel:", "https://wa.me", "https://api.whatsapp")):
                self.tel.append(a["href"])
        if tag in ("p", "br", "li", "div", "h1", "h2", "h3", "h4", "tr", "section", "article"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "template") and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in ("h1", "h2", "h3") and self._hbuf:
            self.h.append((self._hbuf[0], re.sub(r"\s+", " ", self._hbuf[1]).strip()))
            self._hbuf = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        if self._hbuf:
            self._hbuf[1] += data
        self.text.append(data)


def _analyser(texte_html: str) -> _Page:
    p = _Page()
    try:
        p.feed(texte_html)
    except Exception:
        pass
    return p


def lire(url: str, max_caracteres: int = 12000) -> dict:
    final, statut, ent, texte, duree = recuperer(url)
    ctype = ent.get("content-type", "")
    if "html" not in ctype and "xml" not in ctype:
        return {"url": final, "statut": statut, "type": ctype, "titre": "", "description": "",
                "texte": texte[:max_caracteres] if ctype.startswith("text") or "json" in ctype else "(contenu non textuel)"}
    p = _analyser(texte)
    brut = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\r\f\v]+", " ", "".join(p.text))).strip()
    liens = []
    for h in p.links:
        if h.startswith(("http://", "https://", "/")) and not h.startswith("//"):
            liens.append(urllib.parse.urljoin(final, h))
    return {"url": final, "statut": statut, "titre": re.sub(r"\s+", " ", p.title).strip(),
            "description": p.metas.get("description", ""), "texte": brut[:max_caracteres],
            "tronque": len(brut) > max_caracteres, "liens": list(dict.fromkeys(liens))[:40]}


# ------------------------------------------------------------------ recherche
def _nettoyer(s: str) -> str:
    return htmlmod.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _searxng(requete: str, nombre: int) -> list:
    if not config.SEARXNG_URL:
        raise WebErreur("SearXNG non configuré")
    r = httpx.get(config.SEARXNG_URL.rstrip("/") + "/search",
                  params={"q": requete, "format": "json", "language": "fr", "safesearch": 1},
                  headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    res = r.json().get("results", [])
    return [{"titre": x.get("title", ""), "url": x.get("url", ""), "extrait": x.get("content", ""),
             "source": ",".join(x.get("engines", []) or [])} for x in res[:nombre]]


def _duckduckgo(requete: str, nombre: int) -> list:
    r = httpx.post("https://html.duckduckgo.com/html/", data={"q": requete, "kl": "fr-fr"},
                   headers={"User-Agent": UA}, timeout=20)
    blocs = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?(?:class="result__snippet"[^>]*>(.*?)</a>)?',
                       r.text, re.S)
    out = []
    for href, titre, extrait in blocs[:nombre]:
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        out.append({"titre": _nettoyer(titre), "url": href, "extrait": _nettoyer(extrait), "source": "duckduckgo"})
    return out


def chercher(requete: str, nombre: int = 8) -> dict:
    nombre = max(1, min(int(nombre or 8), 20))
    erreurs = []
    for nom, f in (("searxng", _searxng), ("duckduckgo", _duckduckgo)):
        try:
            res = f(requete, nombre)
            if res:
                return {"requete": requete, "moteur": nom, "resultats": res}
            erreurs.append(f"{nom} : aucun résultat")
        except Exception as ex:
            erreurs.append(f"{nom} : {str(ex)[:120]}")
    return {"requete": requete, "moteur": None, "resultats": [], "erreurs": erreurs}


def recherche_approfondie(question: str, pages: int = 3, caracteres_par_page: int = 4000) -> dict:
    """Cherche puis lit les meilleures pages : matière première pour une réponse sourcée."""
    r = chercher(question, 8)
    lues = []
    for x in r["resultats"]:
        if len(lues) >= max(1, min(int(pages), 5)):
            break
        try:
            p = lire(x["url"], caracteres_par_page)
            if len(p.get("texte", "")) > 200:
                lues.append({"titre": p["titre"] or x["titre"], "url": p["url"], "texte": p["texte"]})
        except Exception:
            continue
    return {"question": question, "moteur": r.get("moteur"), "sources": lues,
            "autres_resultats": [x for x in r["resultats"] if x["url"] not in {s["url"] for s in lues}][:5]}


# ------------------------------------------------------------------ audit de site (prospection)
def auditer(url: str) -> dict:
    final, statut, ent, texte, duree = recuperer(url)
    p = _analyser(texte)
    title, desc = re.sub(r"\s+", " ", p.title).strip(), p.metas.get("description", "")
    h1 = [t for tag, t in p.h if tag == "h1"]
    sans_alt = [i.get("src", "")[:70] for i in p.imgs if not i.get("alt", "").strip()]
    reseaux = sorted({d for l in p.links for d in ("facebook", "instagram", "tiktok", "linkedin", "youtube") if d in l.lower()})
    bas = texte.lower()
    controles = [
        ("HTTPS", final.startswith("https://"), "Passer le site en HTTPS (certificat gratuit)."),
        ("La page répond (HTTP 200)", statut == 200, f"La page répond {statut}."),
        ("Rapide (< 1,5 s)", duree < 1.5, f"Réponse en {duree:.1f} s : alléger, mettre en cache, compresser les images."),
        ("Poids raisonnable (< 1,5 Mo de HTML)", len(texte) < 1_500_000, f"HTML de {len(texte) // 1024} Ko."),
        ("Adapté au mobile (viewport)", "viewport" in p.metas, "Ajouter la balise viewport : le site est mal affiché sur téléphone."),
        ("Titre de page 30-60 caractères", 30 <= len(title) <= 60, f"Titre de {len(title)} caractères : « {title[:80]} »."),
        ("Description Google 70-160 caractères", 70 <= len(desc) <= 160, f"Description de {len(desc)} caractères."),
        ("Un seul titre principal (H1)", len(h1) == 1, f"{len(h1)} titre(s) H1."),
        ("Images décrites (alt)", not sans_alt, f"{len(sans_alt)} image(s) sans description."),
        ("Langue déclarée", bool(p.lang), "Ajouter lang=\"fr\"."),
        ("Partage réseaux (Open Graph)", "og:title" in p.metas and "og:image" in p.metas, "Ajouter og:title / og:image : aperçu propre sur Facebook et WhatsApp."),
        ("Fiche entreprise pour Google (JSON-LD)", p.jsonld > 0, "Ajouter les données structurées LocalBusiness (adresse, horaires, téléphone)."),
        ("Bouton appel / WhatsApp", bool(p.tel), "Ajouter un bouton « Appeler » et WhatsApp, indispensable sur mobile."),
        ("Formulaire de contact", p.forms > 0, "Ajouter un formulaire de contact ou de devis."),
        ("Mentions légales", "mentions légales" in bas or "mentions-legales" in bas, "Obligatoires : ajouter une page Mentions légales."),
        ("Liens vers les réseaux sociaux", bool(reseaux), "Relier Facebook / Instagram."),
    ]
    ok = sum(1 for _, v, _ in controles if v)
    return {"url": final, "note": round(100 * ok / len(controles)), "reussis": ok, "total": len(controles),
            "controles": [{"controle": n, "ok": v, "conseil": "" if v else f} for n, v, f in controles],
            "titre": title, "description": desc, "titres": p.h[:12], "reseaux": reseaux, "duree_s": round(duree, 2),
            "generateur": p.metas.get("generator", "")}


# ------------------------------------------------------------------ entreprises (API publique de l'État)
def entreprises(requete: str = "", code_postal: str = "", departement: str = "", naf: str = "", nombre: int = 10) -> dict:
    params = {"per_page": max(1, min(int(nombre or 10), 25)), "etat_administratif": "A"}
    if requete:
        params["q"] = requete
    if code_postal:
        params["code_postal"] = code_postal
    if departement:
        params["departement"] = departement
    if naf:
        params["activite_principale"] = naf
    if not (requete or naf):
        raise WebErreur("Indiquer un nom, une activité ou un code NAF")
    r = httpx.get(API_ENTREPRISES, params=params, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    out = []
    for e in r.json().get("results", []):
        s = e.get("siege") or {}
        out.append({"nom": e.get("nom_complet"), "siren": e.get("siren"), "siret_siege": s.get("siret"),
                    "adresse": s.get("adresse"), "code_postal": s.get("code_postal"), "commune": s.get("libelle_commune"),
                    "naf": e.get("activite_principale"), "categorie": e.get("categorie_entreprise"),
                    "tranche_effectif": e.get("tranche_effectif_salarie"), "creation": e.get("date_creation")})
    return {"total": r.json().get("total_results", len(out)), "entreprises": out}
