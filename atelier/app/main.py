"""PK Atelier — l'atelier de production du Bureau IA PixelKréol.

API (réseau Docker interne, clé obligatoire) :
  POST /api/pieces            facture | devis | avoir en PDF (Factur-X si SIREN renseigné)
  POST /api/pieces/{n}/facturer   transformer un devis en facture
  POST /api/pieces/{n}/payee  marquer une facture payée
  GET  /api/pieces            registre
  POST /api/pdf               document PDF libre (Markdown) dans la charte
  POST /api/mails             préparer un mail (validation par lien, ou envoi direct selon la config)
  GET  /api/mails             suivi des mails
  POST /api/sites/generer     générer + publier un site vitrine démo
  POST /api/sites/publier     publier du HTML (ou plusieurs fichiers / un zip)
  GET  /api/sites · DELETE /api/sites/{slug} · POST /api/sites/{slug}/prolonger
  POST /api/sites/{slug}/production   passer en ligne pour de bon (+ domaine client) : validation par Will
  GET  /api/sites/{slug}/hebergement  état DNS / HTTPS · POST /api/sites/{slug}/exporter  zip
  POST /api/web/chercher · /api/web/lire · /api/web/approfondir · /api/web/auditer · /api/web/entreprises
Public :
  GET /f/{jeton}/{nom}        télécharger un fichier généré
  GET|POST /valider/{id}      page de validation d'un mail
  GET|POST /valider-site/{slug}  page de validation d'une mise en production / d'un retrait
  demo.<domaine>/{slug}/ · {slug}.demo.<domaine>/ · domaine du client   sites hébergés
"""
import asyncio
import hmac
import re
import mimetypes
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import config, hebergement, mail, pdf, sites, store, web

app = FastAPI(title="PK Atelier", version="1.2.0", docs_url=None, redoc_url=None)


def cle(authorization: str = Header(default="")):
    tok = authorization.removeprefix("Bearer ").strip()
    if not config.API_KEY or not hmac.compare_digest(tok, config.API_KEY):
        raise HTTPException(401, "Clé API invalide")


@app.on_event("startup")
async def demarrage():
    store.init()

    async def boucle():
        while True:
            try:
                sites.nettoyer()
            except Exception:
                pass
            await asyncio.sleep(3600)

    asyncio.create_task(boucle())


# ------------------------------------------------------------------ modèles
class Client(BaseModel):
    nom: str
    adresse: str = ""
    email: str = ""
    siren: str = ""
    tva: str = ""
    reference: str = ""
    pays: str = "FR"


class Ligne(BaseModel):
    designation: str
    quantite: float = 1
    prix_unitaire_ht: float
    tva: Optional[float] = None
    detail: str = ""


class Piece(BaseModel):
    type: str = Field("facture", pattern="^(facture|devis|avoir)$")
    client: Client
    lignes: list[Ligne]
    objet: str = ""
    notes: str = ""
    acompte_deja_verse: float = 0
    echeance_jours: int = 30
    validite_jours: int = 30
    date_prestation: str = ""
    ref: str = ""
    kap: str = ""
    conditions: str = ""


class Doc(BaseModel):
    titre: str
    contenu: str
    sous_titre: str = ""
    type: str = "document"


class Mail(BaseModel):
    a: str
    sujet: str
    corps: str
    cc: str = ""
    pieces_jointes: list[str] = []
    agent: str = ""


class SiteGen(BaseModel):
    entreprise: str
    activite: str = ""
    ville: str = ""
    slogan: str = ""
    description: str = ""
    services: list[dict] | str = []
    atouts: list[str] | str = []
    apropos: str = ""
    telephone: str = ""
    email: str = ""
    adresse: str = ""
    horaires: str = ""
    zone: str = ""
    style: str = "lagon"
    couleur_principale: str = ""
    images: list[str] = []
    avis: list[dict] = []
    appel_action: str = ""
    jours: int = 0
    mot_de_passe: str = ""
    sous_domaine: str = ""


class SitePub(BaseModel):
    nom: str
    html: str = ""
    fichiers: dict[str, str] = {}
    zip_base64: str = ""
    jours: int = 0
    mot_de_passe: str = ""
    sous_domaine: str = ""


class Production(BaseModel):
    domaine: str = ""


class Recherche(BaseModel):
    requete: str
    nombre: int = 8


class Lecture(BaseModel):
    url: str
    max_caracteres: int = 12000


class Approfondir(BaseModel):
    question: str
    pages: int = 3


class Entreprises(BaseModel):
    requete: str = ""
    code_postal: str = ""
    departement: str = ""
    naf: str = ""
    nombre: int = 10


# ------------------------------------------------------------------ API
@app.get("/health")
def sante():
    return {"ok": True, "smtp": mail.smtp_configure(), "facturx": bool(config.V["siren"] or config.V["siret"]),
            "validation_mails": config.MAIL_VALIDATION, "hebergement": config.HEBERGEMENT_ACTIF,
            "sous_domaines": config.SOUS_DOMAINES}


@app.post("/api/pieces", dependencies=[Depends(cle)])
def api_piece(p: Piece):
    d = p.model_dump()
    d["lignes"] = [{k: v for k, v in l.items() if v is not None} for l in d["lignes"]]
    if not d["lignes"]:
        raise HTTPException(400, "Aucune ligne")
    return pdf.creer_piece(d)


@app.get("/api/pieces", dependencies=[Depends(cle)])
def api_pieces(type: str = "", statut: str = "", limite: int = 50):
    q, a = "SELECT numero,type,date,client,total_ht,total_tva,total_ttc,statut,fichier,ref FROM pieces WHERE 1=1", []
    if type:
        q += " AND type=?"; a.append(type)
    if statut:
        q += " AND statut=?"; a.append(statut)
    q += " ORDER BY cree_le DESC LIMIT ?"; a.append(limite)
    with store.conn() as c:
        rows = [dict(r) for r in c.execute(q, a).fetchall()]
    for r in rows:
        f = store.fichier(r["fichier"]) if r["fichier"] else None
        r["url_pdf"] = f"{config.URL_PUBLIQUE}/f/{r['fichier']}/{f['nom']}" if f else None
    return rows


@app.post("/api/pieces/{numero}/facturer", dependencies=[Depends(cle)])
def api_facturer(numero: str, acompte_deja_verse: float = 0):
    import json
    with store.conn() as c:
        r = c.execute("SELECT * FROM pieces WHERE numero=? AND type='devis'", (numero,)).fetchone()
    if not r:
        raise HTTPException(404, "Devis introuvable")
    d = json.loads(r["donnees"])
    nouvelle = {k: d[k] for k in ("client", "objet", "notes", "kap") if k in d}
    nouvelle.update(type="facture", ref=numero, acompte_deja_verse=acompte_deja_verse,
                    lignes=[{k: l[k] for k in ("designation", "quantite", "prix_unitaire_ht", "tva", "detail") if k in l} for l in d["lignes"]])
    res = pdf.creer_piece(nouvelle)
    with store.conn() as c:
        c.execute("UPDATE pieces SET statut='accepté' WHERE numero=?", (numero,))
    return res


@app.post("/api/pieces/{numero}/payee", dependencies=[Depends(cle)])
def api_payee(numero: str):
    with store.conn() as c:
        n = c.execute("UPDATE pieces SET statut='payé' WHERE numero=? AND type='facture'", (numero,)).rowcount
    if not n:
        raise HTTPException(404, "Facture introuvable")
    store.journal("facture_payee", numero)
    return {"numero": numero, "statut": "payé"}


@app.post("/api/pdf", dependencies=[Depends(cle)])
def api_pdf(d: Doc):
    return pdf.creer_document(d.titre, d.contenu, d.sous_titre, d.type)


@app.post("/api/mails", dependencies=[Depends(cle)])
def api_mail(m: Mail):
    try:
        return mail.preparer(m.a, m.sujet, m.corps, m.cc, m.pieces_jointes, m.agent)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.get("/api/mails", dependencies=[Depends(cle)])
def api_mails(limite: int = 30):
    with store.conn() as c:
        return [dict(r) | {"lien_validation": mail.lien_validation(r["id"]) if r["statut"] == "à valider" else None}
                for r in c.execute("SELECT id,a,sujet,statut,agent,cree_le,envoye_le,erreur FROM mails ORDER BY cree_le DESC LIMIT ?", (limite,)).fetchall()]


@app.post("/api/sites/generer", dependencies=[Depends(cle)])
def api_site_gen(s: SiteGen):
    d = s.model_dump()
    return {**sites.publier(s.entreprise, {"index.html": sites.generer_html(d)}, s.jours, s.mot_de_passe, s.sous_domaine or s.entreprise), "genere": True}


@app.post("/api/sites/publier", dependencies=[Depends(cle)])
def api_site_pub(s: SitePub):
    try:
        if s.zip_base64:
            return sites.publier_zip(s.nom, s.zip_base64, s.jours, s.mot_de_passe, s.sous_domaine)
        f = dict(s.fichiers)
        if s.html:
            f["index.html"] = s.html
        return sites.publier(s.nom, f, s.jours, s.mot_de_passe, s.sous_domaine)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.get("/api/sites", dependencies=[Depends(cle)])
def api_sites():
    return sites.lister()


@app.delete("/api/sites/{slug}", dependencies=[Depends(cle)])
def api_site_del(slug: str):
    try:
        return {"supprime": sites.supprimer(slug)}
    except PermissionError:
        return {"supprime": False, "validation_requise": True, **sites.demander_validation(slug, "retrait")}


@app.post("/api/sites/{slug}/production", dependencies=[Depends(cle)])
def api_site_prod(slug: str, p: Production):
    try:
        return sites.demander_validation(slug, "production", p.domaine)
    except LookupError as ex:
        raise HTTPException(404, str(ex))
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.get("/api/sites/{slug}/hebergement", dependencies=[Depends(cle)])
def api_site_heb(slug: str):
    r = sites.info(slug)
    if not r:
        raise HTTPException(404, "Site introuvable")
    res = {"slug": slug, "nom": r["nom"], "production": bool(r["production"]), "expire_le": r["expire_le"],
           "hebergement_actif": config.HEBERGEMENT_ACTIF, "sous_domaines": config.SOUS_DOMAINES, **sites.urls(r)}
    if r["domaine"]:
        res["domaine"] = r["domaine"]
        res["dns_ips"] = hebergement.ips(r["domaine"])
        res["dns_ok"] = hebergement.dns_ok(r["domaine"])
        if not res["dns_ok"]:
            res["instructions_dns"] = hebergement.instructions_dns(r["domaine"])
    if r["demande"]:
        import json
        res["demande_en_attente"] = json.loads(r["demande"])
    return res


@app.post("/api/sites/{slug}/exporter", dependencies=[Depends(cle)])
def api_site_export(slug: str):
    try:
        return sites.exporter(slug)
    except LookupError as ex:
        raise HTTPException(404, str(ex))


@app.post("/api/sites/{slug}/prolonger", dependencies=[Depends(cle)])
def api_site_prol(slug: str, jours: int = 30):
    return sites.prolonger(slug, jours)


# ------------------------------------------------------------------ web
def _web(f, *a):
    try:
        return f(*a)
    except web.WebErreur as ex:
        raise HTTPException(400, str(ex))
    except Exception as ex:
        raise HTTPException(502, f"Erreur web : {str(ex)[:200]}")


@app.post("/api/web/chercher", dependencies=[Depends(cle)])
def api_chercher(r: Recherche):
    return _web(web.chercher, r.requete, r.nombre)


@app.post("/api/web/lire", dependencies=[Depends(cle)])
def api_lire(r: Lecture):
    return _web(web.lire, r.url, max(500, min(r.max_caracteres, 40000)))


@app.post("/api/web/approfondir", dependencies=[Depends(cle)])
def api_approfondir(r: Approfondir):
    return _web(web.recherche_approfondie, r.question, r.pages)


@app.post("/api/web/auditer", dependencies=[Depends(cle)])
def api_auditer(r: Lecture):
    return _web(web.auditer, r.url)


@app.post("/api/web/entreprises", dependencies=[Depends(cle)])
def api_entreprises(r: Entreprises):
    return _web(web.entreprises, r.requete, r.code_postal, r.departement, r.naf, r.nombre)


# ------------------------------------------------------------------ public
@app.get("/f/{jeton}/{nom}")
def telecharger(jeton: str, nom: str):
    f = store.fichier(jeton)
    if not f or not Path(f["chemin"]).exists():
        raise HTTPException(404, "Fichier introuvable")
    return FileResponse(f["chemin"], media_type=f["type"], filename=f["nom"],
                        headers={"Content-Disposition": f'inline; filename="{f["nom"]}"', "X-Robots-Tag": "noindex"})


@app.get("/valider/{mid}", response_class=HTMLResponse)
def valider_page(mid: str, t: str = ""):
    return mail.page_validation(mid, t)


@app.post("/valider/{mid}", response_class=HTMLResponse)
def valider_action(mid: str, t: str = Form(""), action: str = Form("")):
    if not mail.verifier_jeton(mid, t):
        raise HTTPException(403, "Lien invalide")
    if action == "envoyer":
        try:
            mail.envoyer(mid)
            msg = "✅ Mail envoyé."
        except Exception as ex:
            msg = f"Échec de l'envoi : {ex}"
    elif action == "annuler":
        mail.annuler(mid)
        msg = "Mail annulé."
    else:
        msg = ""
    return mail.page_validation(mid, t, msg)


def _page_site(slug: str, t: str, message: str = "") -> str:
    import html as H
    import json
    r = sites.info(slug)
    if not r:
        return "<h1>Lien invalide</h1>"
    dem = json.loads(r["demande"]) if r["demande"] else None
    if dem and not hmac.compare_digest(sites.jeton_site(slug, dem["action"], dem.get("domaine", "")), t or ""):
        return "<h1>Lien invalide</h1>"
    if not dem and not message:
        return "<h1>Lien invalide ou déjà utilisé</h1>"
    u = sites.urls(r)
    corps = ""
    if dem:
        if dem["action"] == "production":
            quoi = f"Mettre <b>{H.escape(r['nom'])}</b> en ligne pour de bon" + (f" sur <b>{H.escape(dem['domaine'])}</b>" if dem.get("domaine") else "")
            detail = "<ul><li>le bandeau « maquette » et le blocage Google (noindex) sont retirés ;</li><li>le site n'expire plus ;</li>" + \
                     ("<li>le domaine est branché et le certificat HTTPS se crée tout seul dès que le DNS pointe vers le serveur.</li>" if dem.get("domaine") else "") + "</ul>"
            if dem.get("domaine"):
                detail += f'<p style="background:#FFF6E8;padding:10px;border-radius:10px">{H.escape(hebergement.instructions_dns(dem["domaine"]))}</p>'
            bouton = "✅ OUI, mettre en ligne"
        else:
            quoi = f"Supprimer le site en production <b>{H.escape(r['nom'])}</b>" + (f" ({H.escape(r['domaine'])})" if r["domaine"] else "")
            detail = "<p>Le site et son certificat seront retirés. Pense à exporter le zip avant si besoin.</p>"
            bouton = "🗑 OUI, supprimer"
        corps = f"""<p>{quoi}</p>{detail}<p>Aperçu : <a href="{H.escape(u['url_immediate'])}" target="_blank">{H.escape(u['url_immediate'])}</a></p>
<form method="post" style="display:flex;gap:10px;margin-top:18px"><input type="hidden" name="t" value="{H.escape(t)}">
<button name="action" value="valider" style="flex:1;padding:14px;border:0;border-radius:999px;background:#0B5D6B;color:#fff;font-size:16px">{bouton}</button>
<button name="action" value="annuler" style="flex:1;padding:14px;border:0;border-radius:999px;background:#E4F3F5;color:#0B5D6B;font-size:16px">✖ Annuler</button></form>"""
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Valider un site — PixelKréol</title></head>
<body style="font-family:system-ui,Arial,sans-serif;background:#F3F9FA;margin:0;padding:16px;color:#27302F">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:16px;padding:20px;border:1px solid #D3E2E4">
<div style="font-weight:700;color:#0B5D6B;font-size:20px">Pixel<span style="color:#E0562B">Kréol</span> · validation d'un site</div>
{f'<p style="background:#E6F2EA;padding:10px;border-radius:10px">{H.escape(message)}</p>' if message else ''}{corps}</div></body></html>"""


@app.get("/valider-site/{slug}", response_class=HTMLResponse)
def valider_site_page(slug: str, t: str = ""):
    return _page_site(slug, t)


@app.post("/valider-site/{slug}", response_class=HTMLResponse)
def valider_site_action(slug: str, t: str = Form(""), action: str = Form("")):
    import json
    r = sites.info(slug)
    dem = json.loads(r["demande"]) if r and r["demande"] else None
    if not dem or not hmac.compare_digest(sites.jeton_site(slug, dem["action"], dem.get("domaine", "")), t or ""):
        raise HTTPException(403, "Lien invalide")
    if action == "valider":
        msg = sites.appliquer_validation(slug)
    elif action == "annuler":
        sites.annuler_validation(slug)
        msg = "Demande annulée."
    else:
        msg = ""
    if not sites.info(slug):
        return HTMLResponse(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><p style='font-family:system-ui;padding:20px'>{msg}</p>")
    return _page_site(slug, t, msg)


def _page_mdp(slug: str, erreur: bool = False) -> str:
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>Aperçu protégé</title></head>
<body style="font-family:system-ui,Arial;background:#F3F9FA;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0">
<form method="post" style="background:#fff;padding:26px;border-radius:16px;border:1px solid #D3E2E4;max-width:340px;width:92%">
<div style="font-weight:700;color:#0B5D6B;font-size:20px;margin-bottom:10px">Pixel<span style="color:#E0562B">Kréol</span></div>
<p>Cet aperçu est protégé. Saisissez le mot de passe communiqué par PixelKréol.</p>
{'<p style="color:#E0562B">Mot de passe incorrect.</p>' if erreur else ''}
<input type="password" name="mdp" autofocus style="width:100%;padding:12px;border:1.5px solid #D3E2E4;border-radius:10px;font-size:16px">
<button style="margin-top:12px;width:100%;padding:12px;border:0;border-radius:999px;background:#0B5D6B;color:#fff;font-size:16px">Voir la maquette</button></form></body></html>"""


async def servir_site(request: Request, s, rel_parts: list, prefixe: str):
    """Sert le site s. prefixe = « /slug » (lien immédiat) ou « » (sous-domaine / domaine client)."""
    slug = s["slug"]
    if s["expire_le"] and s["expire_le"] < store.maintenant().isoformat(timespec="seconds"):
        return HTMLResponse("<h1>Cette démo a expiré</h1><p>Contactez PixelKréol pour la réactiver.</p>", status_code=410)
    if s["mot_de_passe"]:
        ok = hmac.compare_digest(request.cookies.get(f"pk_{slug}", ""), sites._cookie_val(slug))
        if request.method == "POST" and not ok:
            form = await request.form()
            if hmac.compare_digest(sites.hash_mdp(form.get("mdp", "")), s["mot_de_passe"]):
                r = RedirectResponse(f"{prefixe}/", status_code=303)
                r.set_cookie(f"pk_{slug}", sites._cookie_val(slug), httponly=True, secure=True, samesite="lax", max_age=30 * 86400)
                return r
            return HTMLResponse(_page_mdp(slug, True), status_code=401)
        if not ok:
            return HTMLResponse(_page_mdp(slug), status_code=401)
    base = (sites.RACINE / slug).resolve()
    rel = "/".join(rel_parts) or "index.html"
    cible = (base / rel).resolve()
    if cible.is_dir():
        cible = cible / "index.html"
    if not str(cible).startswith(str(base) + "/") or not cible.exists():
        return HTMLResponse("<h1>Page introuvable</h1>", status_code=404)
    typ = mimetypes.guess_type(str(cible))[0] or "application/octet-stream"
    prod = bool(s["production"])
    entetes = {"Cache-Control": "public, max-age=300"}
    if not prod:
        entetes["X-Robots-Tag"] = "noindex, nofollow"
    if not prod and cible.suffix.lower() in (".html", ".htm"):
        return HTMLResponse(sites._injecter(cible.read_text("utf-8", "replace")), headers=entetes)
    return FileResponse(cible, media_type=typ, headers=entetes)


async def servir_demo(request: Request, chemin: str):
    parts = [p for p in chemin.split("/") if p]
    if not parts:
        return HTMLResponse("<h1>PixelKréol — démos</h1>", status_code=404)
    slug = parts[0]
    s = sites.info(slug) if re.fullmatch(r"[a-z0-9-]+", slug) else None
    if not s:
        return HTMLResponse("<h1>Démo introuvable ou expirée</h1>", status_code=404)
    if len(parts) == 1 and not chemin.endswith("/"):
        return RedirectResponse(f"/{slug}/")
    return await servir_site(request, s, parts[1:], f"/{slug}")


@app.api_route("/{chemin:path}", methods=["GET", "POST"])
async def aiguillage(request: Request, chemin: str):
    hote = request.headers.get("host", "").split(":")[0].lower()
    if hote == config.HOTE_DEMO:
        return await servir_demo(request, chemin)
    if hote and hote not in (config.URL_PUBLIQUE.split("//")[-1], "localhost", "127.0.0.1", "pk_atelier"):
        s = sites.site_par_hote(hote)
        if s:
            return await servir_site(request, s, [p for p in chemin.split("/") if p], "")
        if hote.endswith("." + config.HOTE_DEMO) or "." in hote:
            return HTMLResponse("<h1>Site introuvable</h1><p>Ce site n'est pas (ou plus) hébergé ici.</p>", status_code=404)
    if chemin.startswith("demo/"):
        return await servir_demo(request, chemin.removeprefix("demo/"))
    if chemin == "":
        return HTMLResponse("<h1>PK Atelier</h1><p>Service interne du Bureau IA PixelKréol.</p>")
    raise HTTPException(404)
