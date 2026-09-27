"""Mails : préparation par les agents, validation par Will (lien sécurisé), envoi SMTP avec pièces jointes."""
import hashlib
import hmac
import html
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

import markdown as md

from . import config, store


def jeton_validation(mid: str) -> str:
    return hmac.new(config.SECRET.encode(), f"mail:{mid}".encode(), hashlib.sha256).hexdigest()[:32]


def verifier_jeton(mid: str, t: str) -> bool:
    return hmac.compare_digest(jeton_validation(mid), t or "")


def lien_validation(mid: str) -> str:
    return f"{config.URL_PUBLIQUE}/valider/{mid}?t={jeton_validation(mid)}"


def _adresses(x) -> list:
    if isinstance(x, str):
        x = x.replace(";", ",").split(",")
    return [a.strip() for a in (x or []) if a and "@" in a]


def smtp_configure() -> bool:
    s = config.SMTP
    return bool(s["hote"] and s["expediteur"])


def _envoyer_smtp(a: list, cc: list, sujet: str, corps_md: str, pieces: list):
    s = config.SMTP
    msg = EmailMessage()
    msg["From"] = formataddr((s["nom_expediteur"], s["expediteur"]))
    msg["To"] = ", ".join(a)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = sujet
    msg["Message-ID"] = make_msgid(domain=s["expediteur"].split("@")[-1])
    msg.set_content(corps_md)
    msg.add_alternative(
        "<html><body style=\"font-family:Arial,sans-serif;font-size:15px;line-height:1.5;color:#27302F\">"
        + md.markdown(corps_md, extensions=["nl2br", "sane_lists"]) + "</body></html>", subtype="html")
    for jeton in pieces:
        f = store.fichier(jeton)
        if not f:
            raise ValueError(f"Pièce jointe introuvable : {jeton}")
        data = Path(f["chemin"]).read_bytes()
        maintype, subtype = (f["type"] or "application/octet-stream").split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=f["nom"])
    ctx = ssl.create_default_context()
    if s["ssl"]:
        with smtplib.SMTP_SSL(s["hote"], s["port"], context=ctx, timeout=30) as srv:
            if s["utilisateur"]:
                srv.login(s["utilisateur"], s["mot_de_passe"])
            srv.send_message(msg)
    else:
        with smtplib.SMTP(s["hote"], s["port"], timeout=30) as srv:
            srv.ehlo()
            if srv.has_extn("starttls"):
                srv.starttls(context=ctx)
                srv.ehlo()
            if s["utilisateur"]:
                srv.login(s["utilisateur"], s["mot_de_passe"])
            srv.send_message(msg)


def _envoyes_aujourdhui() -> int:
    jour = store.maintenant().date().isoformat()
    with store.conn() as c:
        return c.execute("SELECT COUNT(*) n FROM mails WHERE statut='envoyé' AND envoye_le LIKE ?", (jour + "%",)).fetchone()["n"]


def envoyer(mid: str) -> dict:
    with store.conn() as c:
        m = c.execute("SELECT * FROM mails WHERE id=?", (mid,)).fetchone()
    if not m:
        raise ValueError("Mail introuvable")
    if m["statut"] == "envoyé":
        return {"id": mid, "statut": "envoyé", "info": "déjà envoyé"}
    if m["statut"] == "annulé":
        raise ValueError("Mail annulé")
    if not smtp_configure():
        raise ValueError("SMTP non configuré (SMTP_HOTE, SMTP_UTILISATEUR, SMTP_MOT_DE_PASSE dans .env)")
    if _envoyes_aujourdhui() >= config.MAIL_MAX_PAR_JOUR:
        raise ValueError(f"Limite de {config.MAIL_MAX_PAR_JOUR} mails par jour atteinte")
    try:
        _envoyer_smtp(_adresses(m["a"]), _adresses(m["cc"]), m["sujet"], m["corps"], [p for p in (m["pieces"] or "").split(",") if p])
    except Exception as ex:
        with store.conn() as c:
            c.execute("UPDATE mails SET statut='erreur', erreur=? WHERE id=?", (str(ex)[:500], mid))
        store.journal("mail_erreur", {"id": mid, "erreur": str(ex)[:200]})
        raise
    with store.conn() as c:
        c.execute("UPDATE mails SET statut='envoyé', envoye_le=?, erreur=NULL WHERE id=?",
                  (store.maintenant().isoformat(timespec="seconds"), mid))
    store.journal("mail_envoye", {"id": mid, "a": m["a"], "sujet": m["sujet"]})
    return {"id": mid, "statut": "envoyé"}


def preparer(a, sujet: str, corps: str, cc=None, pieces=None, agent: str = "") -> dict:
    dest, copie = _adresses(a), _adresses(cc)
    if not dest:
        raise ValueError("Aucune adresse de destinataire valide")
    pieces = [p.split("/f/")[-1].split("/")[0] if "/f/" in p else p for p in (pieces or []) if p]
    for p in pieces:
        if not store.fichier(p):
            raise ValueError(f"Pièce jointe inconnue : {p}")
    mid = secrets.token_hex(6)
    with store.conn() as c:
        c.execute("INSERT INTO mails(id,a,cc,sujet,corps,pieces,statut,agent,cree_le) VALUES (?,?,?,?,?,?,?,?,?)",
                  (mid, ",".join(dest), ",".join(copie), sujet, corps, ",".join(pieces), "à valider", agent,
                   store.maintenant().isoformat(timespec="seconds")))
    store.journal("mail_prepare", {"id": mid, "a": dest, "sujet": sujet, "agent": agent})
    tous = [x.lower() for x in dest + copie]
    direct = (not config.MAIL_VALIDATION) or (config.MAIL_LISTE_BLANCHE and all(x in config.MAIL_LISTE_BLANCHE for x in tous))
    if direct:
        return {**envoyer(mid), "mode": "direct"}
    res = {"id": mid, "statut": "à valider", "lien_validation": lien_validation(mid), "notification": False}
    if config.MAIL_NOTIFIER and smtp_configure():
        try:
            apercu = corps[:1200]
            _envoyer_smtp([config.MAIL_NOTIFIER], [], f"[À valider] {sujet} → {', '.join(dest)}",
                          f"Un agent du Bureau IA a préparé ce mail{(' (' + agent + ')') if agent else ''} :\n\n"
                          f"**À :** {', '.join(dest)}\n**Objet :** {sujet}\n**Pièces jointes :** {len(pieces)}\n\n---\n\n{apercu}\n\n---\n\n"
                          f"👉 Valider ou refuser : {lien_validation(mid)}", [])
            res["notification"] = True
        except Exception as ex:
            res["notification_erreur"] = str(ex)[:200]
    return res


def page_validation(mid: str, t: str, message: str = "") -> str:
    with store.conn() as c:
        m = c.execute("SELECT * FROM mails WHERE id=?", (mid,)).fetchone()
    if not m or not verifier_jeton(mid, t):
        return "<h1>Lien invalide</h1>"
    pj = []
    for p in (m["pieces"] or "").split(","):
        f = store.fichier(p) if p else None
        if f:
            pj.append(f'<li><a href="/f/{p}/{html.escape(f["nom"])}" target="_blank">{html.escape(f["nom"])}</a></li>')
    etat = {"à valider": "#E8A33D", "envoyé": "#2F7D4A", "annulé": "#5C6866", "erreur": "#E0562B"}.get(m["statut"], "#5C6866")
    boutons = ""
    if m["statut"] in ("à valider", "erreur"):
        boutons = f"""<form method="post" style="display:flex;gap:10px;margin-top:18px">
<input type="hidden" name="t" value="{html.escape(t)}">
<button name="action" value="envoyer" style="flex:1;padding:14px;border:0;border-radius:999px;background:#0B5D6B;color:#fff;font-size:16px">✅ OUI, envoyer</button>
<button name="action" value="annuler" style="flex:1;padding:14px;border:0;border-radius:999px;background:#E4F3F5;color:#0B5D6B;font-size:16px">✖ Annuler</button></form>"""
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Valider un mail — PixelKréol</title></head>
<body style="font-family:system-ui,Arial,sans-serif;background:#F3F9FA;margin:0;padding:16px;color:#27302F">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:16px;padding:20px;border:1px solid #D3E2E4">
<div style="font-weight:700;color:#0B5D6B;font-size:20px">Pixel<span style="color:#E0562B">Kréol</span> · validation d'un mail</div>
{f'<p style="background:#E6F2EA;padding:10px;border-radius:10px">{html.escape(message)}</p>' if message else ''}
<p><span style="background:{etat};color:#fff;border-radius:999px;padding:3px 10px;font-size:13px">{html.escape(m['statut'])}</span>
{('<span style="color:#5C6866;font-size:13px"> préparé par ' + html.escape(m['agent']) + '</span>') if m['agent'] else ''}</p>
<p><b>À :</b> {html.escape(m['a'])}{('<br><b>Cc :</b> ' + html.escape(m['cc'])) if m['cc'] else ''}<br><b>Objet :</b> {html.escape(m['sujet'])}</p>
<div style="border-top:1px solid #D3E2E4;border-bottom:1px solid #D3E2E4;padding:10px 0">{md.markdown(m['corps'], extensions=['nl2br', 'sane_lists'])}</div>
{('<p><b>Pièces jointes :</b></p><ul>' + ''.join(pj) + '</ul>') if pj else ''}
{('<p style="color:#E0562B">Erreur : ' + html.escape(m['erreur']) + '</p>') if m['erreur'] else ''}
{boutons}</div></body></html>"""


def annuler(mid: str):
    with store.conn() as c:
        c.execute("UPDATE mails SET statut='annulé' WHERE id=? AND statut!='envoyé'", (mid,))
    store.journal("mail_annule", {"id": mid})
