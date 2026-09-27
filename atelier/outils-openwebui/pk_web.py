"""
title: PK — Web (lecture de pages, audit SEO, recherche)
author: PixelKréol
description: Lire une page web, auditer son SEO technique et chercher sur le web (DuckDuckGo), sans clé API.
version: 1.0.0
"""

import re
import time
import html as htmlmod
import urllib.parse
from html.parser import HTMLParser

import requests
from pydantic import BaseModel, Field

UA = "Mozilla/5.0 (compatible; PixelKreol-BureauIA/1.0; +https://pixelkreol.mebeeltech.online)"


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.metas, self.h, self.imgs, self.links, self.text = "", {}, [], [], [], []
        self.jsonld, self.lang, self._tag, self._skip, self._hbuf, self._in_title, self._in_ld = 0, "", None, 0, None, False, False
        self.jsonld = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang", "")
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
            if tag == "script" and a.get("type") == "application/ld+json":
                self.jsonld += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            k = (a.get("name") or a.get("property") or "").lower()
            if k:
                self.metas[k] = a.get("content", "")
        if tag == "link" and (a.get("rel") or "").lower() == "canonical":
            self.metas["canonical"] = a.get("href", "")
        if tag in ("h1", "h2", "h3"):
            self._hbuf = [tag, ""]
        if tag == "img":
            self.imgs.append(a)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag in ("p", "br", "li", "div", "h1", "h2", "h3", "tr"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in ("h1", "h2", "h3") and self._hbuf:
            self.h.append((self._hbuf[0], self._hbuf[1].strip()))
            self._hbuf = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        if self._hbuf:
            self._hbuf[1] += data
        self.text.append(data)


class Tools:
    class Valves(BaseModel):
        MAX_CHARS: int = Field(default=12000, description="Longueur maximale du texte renvoyé par lire_page.")

    def __init__(self):
        self.valves = self.Valves()

    def _get(self, url: str):
        if not re.match(r"^https?://", url):
            url = "https://" + url
        t0 = time.time()
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"}, timeout=25, allow_redirects=True)
        return r, time.time() - t0

    def lire_page(self, url: str) -> str:
        """
        Lit une page web et renvoie son titre, sa description et son texte principal (pour résumer un site client, un concurrent, une documentation).
        :param url: Adresse de la page (ex. "https://exemple.re").
        :return: Titre, description et texte de la page.
        """
        r, _ = self._get(url)
        if "html" not in r.headers.get("content-type", ""):
            return f"{r.url} ({r.status_code}) — contenu non HTML : {r.headers.get('content-type')}\n{r.text[: self.valves.MAX_CHARS]}"
        p = _Page()
        p.feed(r.text)
        txt = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", "".join(p.text))).strip()
        return (
            f"URL : {r.url} (HTTP {r.status_code})\nTitre : {p.title.strip()}\n"
            f"Description : {p.metas.get('description', '(aucune)')}\n\n{txt[: self.valves.MAX_CHARS]}"
        )

    def audit_seo_page(self, url: str) -> str:
        """
        Audite le SEO technique d'une page : HTTPS, temps de réponse, balise title, meta description, titres H1-H3, images sans texte alternatif, Open Graph, données structurées, mobile.
        :param url: Adresse de la page à auditer.
        :return: Rapport avec note sur 100 et corrections prioritaires.
        """
        r, dt = self._get(url)
        p = _Page()
        p.feed(r.text)
        title, desc = p.title.strip(), p.metas.get("description", "")
        h1 = [t for tag, t in p.h if tag == "h1"]
        no_alt = [i.get("src", "")[:60] for i in p.imgs if not (i.get("alt") or "").strip()]
        checks = [
            ("HTTPS", r.url.startswith("https://"), "Passer le site en HTTPS (certificat Let's Encrypt gratuit)."),
            ("Réponse HTTP 200", r.status_code == 200, f"La page répond {r.status_code}."),
            ("Temps de réponse < 1,5 s", dt < 1.5, f"Réponse en {dt:.1f} s : alléger la page, activer le cache et la compression."),
            ("Balise title 30-60 caractères", 30 <= len(title) <= 60, f"Title de {len(title)} caractères : « {title[:80]} »."),
            ("Meta description 70-160 caractères", 70 <= len(desc) <= 160, f"Description de {len(desc)} caractères."),
            ("Un seul H1", len(h1) == 1, f"{len(h1)} balise(s) H1 trouvée(s)."),
            ("Viewport mobile", "viewport" in p.metas, "Ajouter <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">."),
            ("Langue déclarée", bool(p.lang), "Ajouter lang=\"fr\" sur la balise <html>."),
            ("Images avec texte alternatif", not no_alt, f"{len(no_alt)} image(s) sans alt : {', '.join(no_alt[:5])}"),
            ("Open Graph (partage réseaux)", "og:title" in p.metas and "og:image" in p.metas, "Ajouter og:title, og:description et og:image."),
            ("Données structurées (JSON-LD)", p.jsonld > 0, "Ajouter un bloc JSON-LD LocalBusiness (nom, adresse, téléphone, horaires)."),
            ("URL canonique", "canonical" in p.metas, "Ajouter <link rel=\"canonical\">."),
        ]
        ok = sum(1 for _, v, _ in checks if v)
        score = round(100 * ok / len(checks))
        lignes = [f"Audit SEO de {r.url} — note {score}/100 ({ok}/{len(checks)} contrôles réussis)\n"]
        lignes += [f"{'✅' if v else '❌'} {n}" + ("" if v else f" — {fix}") for n, v, fix in checks]
        lignes.append("\nStructure des titres : " + " | ".join(f"{t.upper()}: {x[:50]}" for t, x in p.h[:12]))
        return "\n".join(lignes)

    def recherche_web(self, requete: str, nombre: int = 8) -> str:
        """
        Cherche sur le web (DuckDuckGo) et renvoie les premiers résultats : titre, lien et extrait. Utile pour la veille, les concurrents, les prix, l'actualité.
        :param requete: La recherche (ex. "agence web Saint-Pierre Réunion").
        :param nombre: Nombre de résultats (1 à 15).
        :return: Liste de résultats.
        """
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": requete, "kl": "fr-fr"}, headers={"User-Agent": UA}, timeout=25)
        blocs = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?(?:class="result__snippet"[^>]*>(.*?)</a>)?', r.text, re.S)
        out = []
        for href, titre, extrait in blocs[: max(1, min(int(nombre), 15))]:
            if "uddg=" in href:
                href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
            clean = lambda s: htmlmod.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()
            out.append(f"- {clean(titre)}\n  {href}\n  {clean(extrait)}")
        return "\n".join(out) or "Aucun résultat (DuckDuckGo a peut-être limité les requêtes : réessayer plus tard ou activer la recherche web native d'Open WebUI)."
