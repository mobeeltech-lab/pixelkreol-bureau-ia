"""Sites démo : génération d'un site vitrine complet à partir de quelques infos, publication sur un lien partageable."""
import base64
import hashlib
import hmac
import html
import io
import json
import re
import secrets
import shutil
import unicodedata
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from . import config, hebergement, store

RACINE = config.DATA / "sites"
BANDEAU = """<div id="pk-demo" style="position:fixed;left:12px;right:12px;bottom:12px;z-index:99999;background:#1B1A17;color:#fff;font:14px/1.4 system-ui,Arial,sans-serif;border-radius:12px;padding:10px 14px;display:flex;gap:12px;align-items:center;box-shadow:0 10px 30px rgba(0,0,0,.25)"><span style="flex:1">🧪 Maquette de démonstration réalisée par <b>PixelKréol</b> — contenu provisoire, non contractuel.</span><button onclick="document.getElementById('pk-demo').remove()" style="background:#16A6B6;color:#1B1A17;border:0;border-radius:999px;padding:6px 12px;font-weight:700;cursor:pointer">OK</button></div>"""

PALETTES = {
    "lagon": ("#0B5D6B", "#16A6B6", "#E4F3F5", "#1B1A17"),
    "volcan": ("#B23A1A", "#E0562B", "#FFF1EA", "#1B1A17"),
    "vegetal": ("#2F7D4A", "#5FB072", "#EAF5EC", "#16241A"),
    "corail": ("#C2410C", "#FB923C", "#FFF4EC", "#2A1A12"),
    "nuit": ("#1E3A8A", "#3B82F6", "#EEF3FF", "#0F172A"),
    "sobre": ("#27302F", "#5C6866", "#F3F4F4", "#111111"),
}


def slugifier(txt: str) -> str:
    s = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "site"


def _cookie_val(slug: str) -> str:
    return hmac.new(config.SECRET.encode(), f"site:{slug}".encode(), hashlib.sha256).hexdigest()[:32]


def hash_mdp(mdp: str) -> str:
    return hashlib.sha256((config.SECRET + ":" + mdp).encode()).hexdigest()


RESERVES = {"www", "demo", "atelier", "api", "mail", "smtp", "admin", "agents", "n8n", "pixelkreol", "static"}


def jeton_site(slug: str, action: str, domaine: str = "") -> str:
    return hmac.new(config.SECRET.encode(), f"site:{action}:{slug}:{domaine}".encode(), hashlib.sha256).hexdigest()[:32]


def urls(r) -> dict:
    """Adresses d'un site : lien immédiat (chemin), sous-domaine propre, domaine du client."""
    r = dict(r)
    u = {"url_immediate": f"{config.URL_DEMO}/{r['slug']}/"}
    if config.SOUS_DOMAINES:
        h = hebergement.hote_sous_domaine(r["slug"])
        e = hebergement.etat(h) if config.HEBERGEMENT_ACTIF else {"etat": "http_seulement", "https": False}
        u["url_sous_domaine"] = ("https://" if e.get("https") else "http://") + h + "/"
        u["sous_domaine_https"] = bool(e.get("https"))
    if r.get("domaine"):
        e = hebergement.etat(r["domaine"])
        u["url_domaine"] = ("https://" if e.get("https") else "http://") + r["domaine"] + "/"
        u["domaine_etat"] = e.get("etat")
        u["domaine_message"] = e.get("message", "")
    # le meilleur lien à donner au client
    if r.get("domaine") and u.get("domaine_etat") == "actif":
        u["url"] = u["url_domaine"]
    elif u.get("sous_domaine_https"):
        u["url"] = u["url_sous_domaine"]
    else:
        u["url"] = u["url_immediate"]
    return u


def _choisir_slug(nom: str, sous_domaine: str = "") -> str:
    if sous_domaine:
        s = slugifier(sous_domaine)[:40].strip("-")
        if s and s not in RESERVES and not info(s):
            return s
    return f"{slugifier(nom)[:35].strip('-')}-{secrets.token_hex(2)}"


def _injecter(h: str) -> str:
    if "<head" in h.lower() and 'name="robots"' not in h:
        h = re.sub(r"(<head[^>]*>)", r'\1<meta name="robots" content="noindex,nofollow">', h, count=1, flags=re.I)
    if "</body>" in h.lower():
        h = re.sub(r"</body>", BANDEAU + "</body>", h, count=1, flags=re.I)
    else:
        h += BANDEAU
    return h


def publier(nom: str, fichiers: dict, jours: int = 0, mot_de_passe: str = "", sous_domaine: str = "") -> dict:
    """fichiers : {chemin_relatif: contenu str (texte) ou 'base64:...' (binaire)}. index.html obligatoire.
    Les fichiers sont stockés tels quels ; le bandeau « démo » et le noindex sont ajoutés à l'affichage
    tant que le site n'est pas passé en production."""
    if "index.html" not in fichiers:
        raise ValueError("Il faut un fichier index.html")
    slug = _choisir_slug(nom, sous_domaine)
    dossier = RACINE / slug
    dossier.mkdir(parents=True)
    taille = 0
    try:
        for chemin, contenu in fichiers.items():
            rel = Path(chemin.lstrip("/"))
            if ".." in rel.parts or rel.is_absolute():
                raise ValueError(f"Chemin interdit : {chemin}")
            cible = dossier / rel
            cible.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(contenu, str) and contenu.startswith("base64:"):
                data = base64.b64decode(contenu[7:])
            else:
                data = contenu.encode("utf-8")
            taille += len(data)
            if taille > config.SITE_TAILLE_MAX:
                raise ValueError("Site trop volumineux")
            cible.write_bytes(data)
    except Exception:
        shutil.rmtree(dossier, ignore_errors=True)
        raise
    jours = int(jours or config.SITE_JOURS_DEFAUT)
    expire = (store.maintenant() + timedelta(days=jours)).isoformat(timespec="seconds") if jours > 0 else None
    with store.conn() as c:
        c.execute("INSERT INTO sites(slug,nom,expire_le,mot_de_passe,cree_le,taille,production) VALUES (?,?,?,?,?,?,0)",
                  (slug, nom, expire, hash_mdp(mot_de_passe) if mot_de_passe else None, store.maintenant().isoformat(timespec="seconds"), taille))
    store.journal("site_publie", {"slug": slug, "nom": nom})
    if config.SOUS_DOMAINES and config.HEBERGEMENT_ACTIF:
        hebergement.demander(hebergement.hote_sous_domaine(slug), "ajouter", slug, "demo")
    res = {"slug": slug, "expire_le": expire, "protege": bool(mot_de_passe), "octets": taille, "production": False}
    res.update(urls(info(slug)))
    if "url_sous_domaine" in res and not res.get("sous_domaine_https"):
        res["note"] = "Le lien immédiat marche tout de suite ; l'adresse en sous-domaine passe en HTTPS d'ici une à deux minutes (etat_hebergement)."
    return res


def publier_zip(nom: str, zip_b64: str, jours: int = 0, mot_de_passe: str = "", sous_domaine: str = "") -> dict:
    z = zipfile.ZipFile(io.BytesIO(base64.b64decode(zip_b64)))
    fichiers = {}
    noms = [n for n in z.namelist() if not n.endswith("/")]
    prefixe = ""
    if "index.html" not in noms:
        idx = [n for n in noms if n.endswith("/index.html")]
        if idx:
            prefixe = idx[0][: -len("index.html")]
    for n in noms:
        if not n.startswith(prefixe):
            continue
        rel = n[len(prefixe):]
        data = z.read(n)
        if rel.lower().endswith((".html", ".htm", ".css", ".js", ".txt", ".svg", ".json", ".xml")):
            fichiers[rel] = data.decode("utf-8", "replace")
        else:
            fichiers[rel] = "base64:" + base64.b64encode(data).decode()
    return publier(nom, fichiers, jours, mot_de_passe, sous_domaine)


def lister() -> list:
    with store.conn() as c:
        rows = c.execute("SELECT slug, nom, expire_le, mot_de_passe IS NOT NULL protege, cree_le, taille, production, domaine FROM sites ORDER BY cree_le DESC").fetchall()
    return [{**dict(r), **urls(r)} for r in rows]


def supprimer(slug: str, force: bool = False) -> bool:
    slug = slugifier(slug) if not re.fullmatch(r"[a-z0-9-]+", slug) else slug
    r = info(slug)
    if r and r["production"] and not force:
        raise PermissionError("Site en production : le retrait doit être validé par Will")
    shutil.rmtree(RACINE / slug, ignore_errors=True)
    with store.conn() as c:
        n = c.execute("DELETE FROM sites WHERE slug=?", (slug,)).rowcount
    if r and config.HEBERGEMENT_ACTIF:
        if config.SOUS_DOMAINES:
            hebergement.demander(hebergement.hote_sous_domaine(slug), "retirer", slug, "demo")
        if r["domaine"]:
            hebergement.demander(r["domaine"], "retirer", slug, "client")
    store.journal("site_supprime", {"slug": slug})
    return n > 0


# ------------------------------------------------------------------ production (validée par Will)
def demander_validation(slug: str, action: str, domaine: str = "") -> dict:
    """action = production (retirer le bandeau, rendre permanent, brancher un domaine) ou retrait (supprimer un site en production)."""
    r = info(slug)
    if not r:
        raise LookupError("Site introuvable")
    if action == "production" and domaine:
        domaine = hebergement.normaliser(domaine)
        if domaine == config.HOTE_DEMO or domaine.endswith("." + config.HOTE_DEMO) or domaine in (
                config.URL_PUBLIQUE.split("//")[-1], config.URL_DEMO.split("//")[-1]):
            raise ValueError("Ce domaine est réservé à PixelKréol")
        with store.conn() as c:
            if c.execute("SELECT 1 FROM sites WHERE domaine=? AND slug!=?", (domaine, slug)).fetchone():
                raise ValueError("Ce domaine est déjà branché sur un autre site")
    dem = {"action": action, "domaine": domaine, "quand": store.maintenant().isoformat(timespec="seconds")}
    with store.conn() as c:
        c.execute("UPDATE sites SET demande=? WHERE slug=?", (json.dumps(dem), slug))
    lien = f"{config.URL_PUBLIQUE}/valider-site/{slug}?t={jeton_site(slug, action, domaine)}"
    store.journal("site_validation_demandee", {"slug": slug, **dem})
    res = {"slug": slug, "action": action, "domaine": domaine, "lien_validation": lien, "notification": False}
    if domaine:
        res["dns_ok"] = hebergement.dns_ok(domaine)
        res["instructions_dns"] = hebergement.instructions_dns(domaine)
    from . import mail
    if config.MAIL_NOTIFIER and mail.smtp_configure():
        quoi = (f"mettre en ligne « {r['nom']} »" + (f" sur {domaine}" if domaine else " en version définitive")) if action == "production" \
            else f"supprimer le site en production « {r['nom']} »"
        try:
            mail._envoyer_smtp([config.MAIL_NOTIFIER], [], f"[À valider] Site : {quoi}",
                               f"Un agent du Bureau IA demande de **{quoi}**.\n\nAperçu : {urls(r)['url_immediate']}\n\n👉 Valider ou refuser : {lien}", [])
            res["notification"] = True
        except Exception as ex:
            res["notification_erreur"] = str(ex)[:200]
    return res


def appliquer_validation(slug: str) -> str:
    r = info(slug)
    if not r or not r["demande"]:
        return "Aucune demande en attente."
    dem = json.loads(r["demande"])
    if dem["action"] == "retrait":
        supprimer(slug, force=True)
        return "🗑 Site retiré."
    ancien = r["domaine"]
    with store.conn() as c:
        c.execute("UPDATE sites SET production=1, expire_le=NULL, domaine=?, demande=NULL WHERE slug=?",
                  (dem.get("domaine") or ancien, slug))
    if dem.get("domaine") and config.HEBERGEMENT_ACTIF:
        if ancien and ancien != dem["domaine"]:
            hebergement.demander(ancien, "retirer", slug, "client")
        hebergement.demander(dem["domaine"], "ajouter", slug, "client")
    store.journal("site_production", {"slug": slug, "domaine": dem.get("domaine")})
    return "✅ Site passé en production" + (f" sur {dem['domaine']}" if dem.get("domaine") else "") + "."


def annuler_validation(slug: str):
    with store.conn() as c:
        c.execute("UPDATE sites SET demande=NULL WHERE slug=?", (slug,))


def site_par_hote(hote: str):
    """Retrouve le site servi pour un nom d'hôte (sous-domaine démo ou domaine client)."""
    hote = hote.lower().rstrip(".")
    if hote.endswith("." + config.HOTE_DEMO):
        slug = hote[: -len(config.HOTE_DEMO) - 1]
        if re.fullmatch(r"[a-z0-9-]+", slug):
            return info(slug)
        return None
    nu = hote[4:] if hote.startswith("www.") else hote
    with store.conn() as c:
        return c.execute("SELECT * FROM sites WHERE domaine=? AND production=1", (nu,)).fetchone()


def exporter(slug: str) -> dict:
    """Zip des fichiers d'origine (sans bandeau), à remettre au client ou à héberger ailleurs."""
    r = info(slug)
    if not r:
        raise LookupError("Site introuvable")
    base = RACINE / slug
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(base.rglob("*")):
            if f.is_file() and not f.is_symlink():
                z.write(f, f.relative_to(base).as_posix())
    nom = f"{slug}.zip"
    chemin = config.DATA / "fichiers" / f"{secrets.token_hex(6)}-{nom}"
    chemin.write_bytes(buf.getvalue())
    jeton = store.ajouter_fichier(nom, str(chemin), "application/zip", len(buf.getvalue()))
    return {"slug": slug, "url_zip": f"{config.URL_PUBLIQUE}/f/{jeton}/{nom}", "fichier_id": jeton, "octets": len(buf.getvalue())}


def prolonger(slug: str, jours: int) -> dict:
    exp = (store.maintenant() + timedelta(days=int(jours))).isoformat(timespec="seconds") if int(jours) > 0 else None
    with store.conn() as c:
        c.execute("UPDATE sites SET expire_le=? WHERE slug=?", (exp, slug))
    return {"slug": slug, "expire_le": exp}


def nettoyer() -> int:
    maint = store.maintenant().isoformat(timespec="seconds")
    with store.conn() as c:
        rows = c.execute("SELECT slug FROM sites WHERE expire_le IS NOT NULL AND expire_le < ?", (maint,)).fetchall()
    for r in rows:
        supprimer(r["slug"])
    return len(rows)


def info(slug: str):
    with store.conn() as c:
        return c.execute("SELECT * FROM sites WHERE slug=?", (slug,)).fetchone()


# ------------------------------------------------------------------ générateur
def generer_html(d: dict) -> str:
    e = lambda x: html.escape(str(x or ""))
    p1, p2, pale, fonce = PALETTES.get((d.get("style") or "lagon").lower(), PALETTES["lagon"])
    if d.get("couleur_principale") and re.fullmatch(r"#[0-9a-fA-F]{6}", d["couleur_principale"]):
        p1 = d["couleur_principale"]
    nom, activite, ville = d["entreprise"], d.get("activite", ""), d.get("ville", "")
    tel = re.sub(r"[^\d+]", "", d.get("telephone") or "")
    wa = tel.replace("+", "")
    if wa.startswith("0"):
        wa = "262" + wa[1:] if wa.startswith(("0262", "0692", "0693")) else "33" + wa[1:]
    services = d.get("services") or []
    if isinstance(services, str):
        services = [{"titre": s.split(":")[0].strip(), "texte": s.split(":", 1)[1].strip() if ":" in s else ""} for s in services.split("\n") if s.strip()]
    atouts = d.get("atouts") or []
    if isinstance(atouts, str):
        atouts = [a.strip() for a in atouts.split("\n") if a.strip()]
    avis = d.get("avis") or []
    images = [u for u in (d.get("images") or []) if isinstance(u, str) and u.startswith(("https://", "http://"))]
    titre_seo = f"{nom} — {activite} à {ville}" if ville else f"{nom} — {activite}"
    desc = d.get("description") or f"{nom}, {activite} à {ville}. Contactez-nous pour un devis."
    ld = {"@context": "https://schema.org", "@type": "LocalBusiness", "name": nom, "description": desc[:300],
          "address": {"@type": "PostalAddress", "streetAddress": d.get("adresse", ""), "addressLocality": ville, "addressRegion": "La Réunion", "addressCountry": "FR"}}
    if tel:
        ld["telephone"] = tel
    if d.get("email"):
        ld["email"] = d["email"]
    icones = ["✦", "◆", "●", "▲", "■", "★"]
    serv_html = "".join(
        f'<article class="card"><div class="ic">{icones[i % 6]}</div><h3>{e(s.get("titre"))}</h3><p>{e(s.get("texte"))}</p>'
        + (f'<p class="px">{e(s.get("prix"))}</p>' if s.get("prix") else "") + "</article>"
        for i, s in enumerate(services))
    at_html = "".join(f"<li>{e(a)}</li>" for a in atouts)
    avis_html = "".join(f'<blockquote><p>« {e(a.get("texte"))} »</p><footer>— {e(a.get("auteur"))}</footer></blockquote>' for a in avis if a.get("texte"))
    gal_html = "".join(f'<img src="{e(u)}" alt="{e(nom)}" loading="lazy">' for u in images[:6])
    horaires = d.get("horaires") or ""
    zone = d.get("zone") or ""
    cta = d.get("appel_action") or "Demander un devis"
    mots = [w for w in re.findall(r"[A-Za-zÀ-ÿ]+", nom) if w.lower() not in ("de", "du", "des", "la", "le", "les", "et", "l", "d", "au", "aux")]
    initiales = "".join(w[0] for w in mots[:2]).upper() or "★"
    return f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(titre_seo)[:70]}</title><meta name="description" content="{e(desc)[:158]}">
<meta property="og:title" content="{e(titre_seo)}"><meta property="og:description" content="{e(desc)[:200]}"><meta property="og:type" content="website">
{f'<meta property="og:image" content="{e(images[0])}">' if images else ''}
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Outfit:wght@500;700&family=Source+Sans+3:wght@400;600&display=swap" rel="stylesheet">
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>
<style>
:root{{--p1:{p1};--p2:{p2};--pale:{pale};--fonce:{fonce}}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;font-family:"Source Sans 3",system-ui,sans-serif;font-size:18px;line-height:1.6;color:#27302F;background:#fff}}
h1,h2,h3{{font-family:Outfit,system-ui,sans-serif;line-height:1.15;color:var(--fonce);margin:0 0 .5em}}
h1{{font-size:clamp(2.1rem,6vw,3.6rem)}}h2{{font-size:clamp(1.6rem,4vw,2.4rem)}}
a{{color:var(--p1)}}.wrap{{width:min(1100px,92vw);margin:0 auto}}section{{padding:clamp(56px,9vw,100px) 0}}
header{{position:sticky;top:0;background:rgba(255,255,255,.94);backdrop-filter:blur(8px);border-bottom:1px solid #e6ecec;z-index:10}}
nav{{display:flex;justify-content:space-between;align-items:center;height:66px}}.brand{{font-family:Outfit;font-weight:700;font-size:1.3rem;color:var(--p1);text-decoration:none}}
nav ul{{display:flex;gap:22px;list-style:none;margin:0;padding:0}}nav ul a{{text-decoration:none;color:#27302F;font-weight:600}}
@media(max-width:760px){{nav ul{{display:none}}}}@media(max-width:520px){{nav .btn{{display:none}}}}
.btn{{display:inline-block;background:var(--p1);color:#fff;text-decoration:none;font-family:Outfit;font-weight:500;padding:.85em 1.5em;border-radius:999px;transition:transform .2s}}
.btn:hover{{transform:translateY(-2px)}}.btn.s{{background:transparent;color:var(--p1);border:2px solid var(--p1)}}
.hero{{background:linear-gradient(160deg,var(--pale),#fff 70%);padding-top:clamp(50px,8vw,90px)}}
.hero p.lead{{font-size:1.25rem;max-width:55ch;color:#475452}}.hero .g{{display:grid;grid-template-columns:1.2fr .8fr;gap:40px;align-items:center}}@media(max-width:800px){{.hero .g{{grid-template-columns:1fr}}}}.visu{{aspect-ratio:4/3;border-radius:28px;background:linear-gradient(135deg,var(--p1),var(--p2));display:flex;align-items:center;justify-content:center;color:#fff;font-family:Outfit;font-weight:700;font-size:clamp(3rem,9vw,6rem);box-shadow:0 30px 60px -30px var(--p1);overflow:hidden}}.visu img{{width:100%;height:100%;object-fit:cover}}.cta{{display:flex;gap:12px;flex-wrap:wrap;margin-top:22px}}
.tag{{display:inline-block;background:#fff;border:1px solid #dfe8e8;border-radius:999px;padding:4px 12px;font-size:.9rem;color:var(--p1);margin-bottom:14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px;margin-top:30px}}
.card{{background:var(--pale);border-radius:18px;padding:24px}}.card .ic{{font-size:1.4rem;color:var(--p2)}}.card p{{margin:0;color:#475452}}.card .px{{margin-top:10px;font-family:Outfit;color:var(--p1)}}
.about{{display:grid;grid-template-columns:1.1fr .9fr;gap:40px;align-items:start}}@media(max-width:800px){{.about{{grid-template-columns:1fr}}}}
.about ul{{padding-left:0;list-style:none}}.about li{{padding:10px 0 10px 32px;border-bottom:1px solid #e6ecec;position:relative}}.about li:before{{content:"✓";position:absolute;left:4px;color:var(--p2);font-weight:700}}
.gal{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:24px}}.gal img{{width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:14px}}
blockquote{{margin:0;background:#fff;border-left:5px solid var(--p2);border-radius:12px;padding:20px;box-shadow:0 8px 30px rgba(0,0,0,.05)}}blockquote footer{{color:#5C6866;font-size:.95rem}}
.dark{{background:var(--fonce);color:#e8eeee}}.dark h2,.dark h3{{color:#fff}}.dark a{{color:#fff}}
.contact{{display:grid;grid-template-columns:1fr 1fr;gap:34px}}@media(max-width:800px){{.contact{{grid-template-columns:1fr}}}}
.info p{{margin:0 0 10px}}form{{display:grid;gap:12px;background:#fff;border-radius:18px;padding:22px;color:#27302F}}
input,textarea{{font:inherit;padding:.7em .9em;border:1.5px solid #d6e0e0;border-radius:10px;width:100%}}textarea{{min-height:110px}}
footer.site{{padding:30px 0 90px;font-size:.95rem;color:#5C6866}}
.float{{position:fixed;right:16px;bottom:84px;display:flex;flex-direction:column;gap:10px;z-index:20}}
.float a{{width:54px;height:54px;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;text-decoration:none;font-size:1.4rem;box-shadow:0 8px 20px rgba(0,0,0,.2)}}
</style></head><body>
<header><nav class="wrap"><a class="brand" href="#">{e(nom)}</a><ul><li><a href="#services">Services</a></li><li><a href="#apropos">À propos</a></li><li><a href="#contact">Contact</a></li></ul>
<a class="btn" href="#contact" style="padding:.55em 1.1em">{e(cta)}</a></nav></header>
<main>
<section class="hero"><div class="wrap g"><div><span class="tag">{e(activite)}{(' · ' + e(ville)) if ville else ''}</span>
<h1>{e(d.get('slogan') or nom)}</h1><p class="lead">{e(desc)}</p>
<div class="cta"><a class="btn" href="#contact">{e(cta)}</a>{f'<a class="btn s" href="tel:{e(tel)}">📞 {e(d.get("telephone"))}</a>' if tel else ''}</div></div>
<div class="visu">{f'<img src="{e(images[0])}" alt="{e(nom)}">' if images else e(initiales)}</div></div></section>
{f'<section id="services"><div class="wrap"><h2>Nos services</h2><div class="grid">{serv_html}</div></div></section>' if services else ''}
<section id="apropos" style="background:var(--pale)"><div class="wrap about"><div><h2>{e(d.get('titre_apropos') or 'Pourquoi nous choisir')}</h2><p>{e(d.get('apropos') or desc)}</p></div>
<div>{f'<ul>{at_html}</ul>' if at_html else ''}{f'<p><b>Zone d’intervention :</b> {e(zone)}</p>' if zone else ''}{f'<p><b>Horaires :</b> {e(horaires)}</p>' if horaires else ''}</div></div></section>
{f'<section><div class="wrap"><h2>En images</h2><div class="gal">{gal_html}</div></div></section>' if gal_html else ''}
{f'<section><div class="wrap"><h2>Ils nous font confiance</h2><div class="grid">{avis_html}</div></div></section>' if avis_html else ''}
<section id="contact" class="dark"><div class="wrap contact"><div class="info"><h2>Contact</h2>
{f'<p>📍 {e(d.get("adresse"))}{(", " + e(ville)) if ville else ""}</p>' if d.get('adresse') else ''}
{f'<p>📞 <a href="tel:{e(tel)}">{e(d.get("telephone"))}</a></p>' if tel else ''}
{f'<p>✉️ <a href="mailto:{e(d.get("email"))}">{e(d.get("email"))}</a></p>' if d.get('email') else ''}
{f'<p>🕒 {e(horaires)}</p>' if horaires else ''}</div>
<form onsubmit="event.preventDefault();this.innerHTML='<p><b>Merci !</b> (Formulaire de démonstration : sur le site définitif, votre message nous sera transmis.)</p>'">
<label>Votre nom<input required></label><label>Téléphone ou e-mail<input required></label><label>Votre besoin<textarea></textarea></label>
<button class="btn" style="border:0;cursor:pointer">Envoyer</button></form></div></section>
</main>
<footer class="site"><div class="wrap">© {store.maintenant().year} {e(nom)}{(' — ' + e(ville)) if ville else ''} · Mentions légales et politique de confidentialité à compléter · Site réalisé par PixelKréol</div></footer>
<div class="float">{f'<a href="https://wa.me/{e(wa)}" style="background:#25D366" aria-label="WhatsApp">💬</a>' if wa else ''}{f'<a href="tel:{e(tel)}" style="background:var(--p1)" aria-label="Appeler">📞</a>' if tel else ''}</div>
</body></html>"""
