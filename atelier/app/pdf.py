"""Rendu PDF dans la charte PixelKréol : factures, devis, avoirs (Factur-X) et documents libres (Markdown)."""
import html
import io
from datetime import date, timedelta
from pathlib import Path

import markdown as md
from weasyprint import HTML, CSS
from weasyprint.text.fonts import FontConfiguration

from . import config, store, facturx_xml

FONTS = Path(__file__).resolve().parent.parent / "fonts"
FC = FontConfiguration()

BASE_CSS = f"""
@font-face{{font-family:Outfit;font-weight:500;src:url('file://{FONTS}/outfit-latin-500-normal.woff2')}}
@font-face{{font-family:Outfit;font-weight:700;src:url('file://{FONTS}/outfit-latin-700-normal.woff2')}}
@font-face{{font-family:'Source Sans 3';font-weight:400;src:url('file://{FONTS}/source-sans-3-latin-400-normal.woff2')}}
@font-face{{font-family:'Source Sans 3';font-weight:400;font-style:italic;src:url('file://{FONTS}/source-sans-3-latin-400-italic.woff2')}}
@font-face{{font-family:'Source Sans 3';font-weight:600;src:url('file://{FONTS}/source-sans-3-latin-600-normal.woff2')}}
@font-face{{font-family:'Source Sans 3';font-weight:700;src:url('file://{FONTS}/source-sans-3-latin-700-normal.woff2')}}
@page{{size:A4;margin:16mm 15mm 20mm;@bottom-center{{content:"{html.escape(config.V['nom'])} · page " counter(page) " / " counter(pages);font:8pt 'Source Sans 3';color:#5C6866}}}}
:root{{--lagon:#0B5D6B;--vif:#16A6B6;--pale:#E4F3F5;--volcan:#1B1A17;--encre:#27302F;--gris:#5C6866;--corail:#E0562B;--ligne:#D3E2E4}}
body{{font-family:'Source Sans 3',sans-serif;font-size:10pt;line-height:1.45;color:#27302F;margin:0}}
h1,h2,h3{{font-family:Outfit,sans-serif;color:#1B1A17;margin:0 0 6px;line-height:1.15}}
h1{{font-size:22pt}}h2{{font-size:14pt;color:#0B5D6B;margin-top:14px}}h3{{font-size:11.5pt;margin-top:10px}}
p{{margin:0 0 7px}}ul,ol{{margin:3px 0 8px 18px;padding:0}}
table{{width:100%;border-collapse:collapse;margin:6px 0}}
th{{background:#0B5D6B;color:#fff;text-align:left;padding:6px 8px;font-family:Outfit;font-weight:500;font-size:9.5pt}}
td{{padding:6px 8px;border-bottom:1px solid #D3E2E4;vertical-align:top}}
.logo{{font-family:Outfit;font-weight:700;font-size:18pt;color:#0B5D6B}}.logo span{{color:#E0562B}}
.muted{{color:#5C6866}}.r{{text-align:right}}.small{{font-size:8.5pt}}
.box{{background:#E4F3F5;border-radius:8px;padding:9px 12px}}
.warn{{background:#FFF6E8;border-left:4px solid #E0562B;padding:7px 11px;border-radius:0 8px 8px 0}}
code{{font-family:monospace;background:#E4F3F5;padding:0 3px;border-radius:3px}}
blockquote{{border-left:4px solid #16A6B6;margin:6px 0;padding:4px 10px;background:#F3F9FA}}
"""


def eur(x: float) -> str:
    return f"{x:,.2f} €".replace(",", " ").replace(".", ",")


def _logo() -> str:
    nom = config.V["nom"]
    if nom.lower().startswith("pixelkr"):
        return '<div class="logo">Pixel<span>Kréol</span></div>'
    return f'<div class="logo">{html.escape(nom)}</div>'


def calculer(p: dict) -> dict:
    """Calcule lignes, ventilation par taux de TVA et totaux (arrondi à la ligne)."""
    lignes, vent = [], {}
    for l in p["lignes"]:
        q = float(l.get("quantite", 1))
        pu = float(l["prix_unitaire_ht"])
        taux = 0.0 if config.FRANCHISE_TVA else float(l.get("tva", config.TVA_DEFAUT))
        ht = round(q * pu, 2)
        if p["type"] == "avoir":
            ht = -abs(ht)
        lignes.append({**l, "quantite": q, "prix_unitaire_ht": pu, "tva": taux, "total_ht": ht})
        vent.setdefault(taux, 0.0)
        vent[taux] = round(vent[taux] + ht, 2)
    ventilation = [{"taux": t, "base": b, "tva": round(b * t / 100, 2)} for t, b in sorted(vent.items())]
    ht = round(sum(v["base"] for v in ventilation), 2)
    tva = round(sum(v["tva"] for v in ventilation), 2)
    ttc = round(ht + tva, 2)
    acompte = round(float(p.get("acompte_deja_verse") or 0), 2)
    return {**p, "lignes": lignes, "ventilation": ventilation,
            "totaux": {"ht": ht, "tva": tva, "ttc": ttc, "acompte": acompte, "a_payer": round(ttc - acompte, 2)}}


def avertissements_vendeur() -> list:
    v, w = config.V, []
    if not (v["siret"] or v["siren"]):
        w.append("SIREN/SIRET du vendeur non renseigné : mention obligatoire manquante (à compléter dans .env dès l'immatriculation).")
    if not v["adresse"] or v["adresse"] == "La Réunion":
        w.append("Adresse complète du vendeur non renseignée.")
    if not config.FRANCHISE_TVA and not v["tva"]:
        w.append("N° de TVA intracommunautaire du vendeur non renseigné.")
    return w


def _html_piece(p: dict, avert: list) -> str:
    v, c, t = config.V, p["client"], p["totaux"]
    titre = {"facture": "Facture", "devis": "Devis", "avoir": "Avoir"}[p["type"]]
    ech = ("Échéance : " + date.fromisoformat(p["echeance"]).strftime("%d/%m/%Y")) if p.get("echeance") and p["type"] != "devis" else ""
    val = ("Valable jusqu'au : " + date.fromisoformat(p["validite"]).strftime("%d/%m/%Y")) if p["type"] == "devis" else ""
    ref = ("<br>Réf. : " + html.escape(p["ref"])) if p.get("ref") else ""
    if p["type"] != "devis":
        ref = "<br>Date de la prestation : " + html.escape(p.get("date_prestation") or date.fromisoformat(p["date"]).strftime("%d/%m/%Y")) + ref
    nl = "\n"
    adr_client = html.escape(c.get("adresse", "")).replace(nl, "<br>")
    L = []
    L.append(f"""<table style="margin:0"><tr><td style="border:0;padding:0;width:55%">{_logo()}
<div class="small muted">{html.escape(v['forme'])}<br>{html.escape(v['adresse'])}<br>{html.escape(v['cp_ville'])}<br>
{html.escape(v['email'])} {('· ' + html.escape(v['tel'])) if v['tel'] else ''}<br>
{('SIRET ' + html.escape(v['siret'])) if v['siret'] else ('SIREN ' + html.escape(v['siren'])) if v['siren'] else '<b style="color:#E0562B">SIRET à compléter</b>'}
{(' · ' + html.escape(v['rcs'])) if v['rcs'] else ''}{(' · TVA ' + html.escape(v['tva'])) if v['tva'] else ''}</div></td>
<td style="border:0;padding:0" class="r"><h1>{titre}</h1><div style="font-family:Outfit;font-size:12pt;color:#0B5D6B">{html.escape(p['numero'])}</div>
<div class="small">Date : {date.fromisoformat(p['date']).strftime('%d/%m/%Y')}<br>
{ech}{val}{ref}</div></td></tr></table>""")
    L.append(f"""<div class="box" style="margin:12px 0 10px;width:55%;margin-left:45%"><b>{html.escape(c['nom'])}</b><br>
{adr_client}{('<br>SIREN ' + html.escape(c['siren'])) if c.get('siren') else ''}
{('<br>' + html.escape(c['email'])) if c.get('email') else ''}</div>""")
    if p.get("objet"):
        L.append(f"<p><b>Objet :</b> {html.escape(p['objet'])}</p>")
    L.append('<table><tr><th>Désignation</th><th class="r" style="width:9%">Qté</th><th class="r" style="width:15%">PU HT</th>'
             '<th class="r" style="width:9%">TVA</th><th class="r" style="width:16%">Total HT</th></tr>')
    for l in p["lignes"]:
        det = f'<br><span class="small muted">{html.escape(l["detail"])}</span>' if l.get("detail") else ""
        L.append(f"<tr><td>{html.escape(l['designation'])}{det}</td><td class='r'>{l['quantite']:g}</td>"
                 f"<td class='r'>{eur(l['prix_unitaire_ht'])}</td><td class='r'>{str(l['tva']).replace('.', ',').rstrip('0').rstrip(',')} %</td>"
                 f"<td class='r'>{eur(l['total_ht'])}</td></tr>")
    L.append("</table>")
    rows = [f"<tr><td>Total HT</td><td class='r'>{eur(t['ht'])}</td></tr>"]
    for vv in p["ventilation"]:
        lab = "TVA non applicable" if vv["taux"] == 0 else f"TVA {str(vv['taux']).replace('.', ',').rstrip('0').rstrip(',')} % sur {eur(vv['base'])}"
        rows.append(f"<tr><td>{lab}</td><td class='r'>{eur(vv['tva'])}</td></tr>")
    rows.append(f"<tr><td style='font-family:Outfit;font-size:11.5pt;color:#0B5D6B'>Total TTC</td><td class='r' style='font-family:Outfit;font-size:11.5pt;color:#0B5D6B'>{eur(t['ttc'])}</td></tr>")
    if t["acompte"]:
        rows.append(f"<tr><td>Acompte déjà versé</td><td class='r'>- {eur(t['acompte'])}</td></tr>")
        rows.append(f"<tr><td><b>Net à payer</b></td><td class='r'><b>{eur(t['a_payer'])}</b></td></tr>")
    L.append(f'<table style="width:48%;margin-left:52%">{"".join(rows)}</table>')
    if config.FRANCHISE_TVA:
        L.append('<p class="small"><b>TVA non applicable, art. 293 B du CGI.</b></p>')
    if p.get("kap"):
        L.append(f'<div class="box small" style="margin-top:8px"><b>Financement Kap Numérik (estimation) :</b> {html.escape(p["kap"])} '
                 "— sous réserve d'acceptation du dossier par la Région Réunion.</div>")
    if p.get("notes"):
        L.append('<p class="small" style="margin-top:8px">' + html.escape(p["notes"]).replace(nl, "<br>") + '</p>')
    if p["type"] == "devis":
        L.append(f"""<div style="margin-top:14px" class="small"><b>Conditions :</b> {html.escape(p.get('conditions') or 'Acompte de 30 % à la commande, solde à la livraison. Devis valable 30 jours.')}</div>
<table style="margin-top:10px;width:60%"><tr><td style="height:60px;border:1px solid #D3E2E4">Bon pour accord (date, signature, cachet) :</td></tr></table>""")
    else:
        pay = []
        if v["iban"]:
            pay.append(f"Virement : IBAN {html.escape(v['iban'])}{(' · BIC ' + html.escape(v['bic'])) if v['bic'] else ''}")
        pay.append(f"Référence à indiquer : {html.escape(p['numero'])}")
        L.append(f'<div style="margin-top:12px" class="small"><b>Règlement :</b> {" · ".join(pay)}<br>{html.escape(config.PENALITES)}</div>')
    if avert:
        L.append('<div class="warn small" style="margin-top:10px"><b>Document de travail — mentions à compléter :</b><br>'
                 + "<br>".join(html.escape(a) for a in avert) + "</div>")
    return "<html><head><meta charset='utf-8'></head><body>" + "".join(L) + "</body></html>"


def rendre_pdf(html_doc: str, pdf_a: bool = False) -> bytes:
    kw = {"pdf_variant": "pdf/a-3b"} if pdf_a else {}
    return HTML(string=html_doc, base_url=str(FONTS)).write_pdf(stylesheets=[CSS(string=BASE_CSS, font_config=FC)], font_config=FC, **kw)


def creer_piece(p: dict) -> dict:
    """p : type (facture|devis|avoir), client, lignes, objet, notes, acompte_deja_verse, echeance_jours, ref, kap."""
    prefixe = {"facture": "F", "devis": "D", "avoir": "A"}[p["type"]]
    auj = store.maintenant().date()
    p = {**p, "date": auj.isoformat()}
    if p["type"] == "devis":
        p["validite"] = (auj + timedelta(days=int(p.get("validite_jours") or 30))).isoformat()
    else:
        j = min(int(p.get("echeance_jours") or 30), 60)
        p["echeance"] = (auj + timedelta(days=j)).isoformat()
    p = calculer(p)
    p["numero"] = store.prochain_numero(prefixe)
    avert = avertissements_vendeur()
    pdf = rendre_pdf(_html_piece(p, avert), pdf_a=(p["type"] != "devis"))
    facturx_ok = False
    if p["type"] != "devis" and (config.V["siren"] or config.V["siret"]):
        from facturx import generate_from_binary
        v = dict(config.V)
        v["siren"] = v["siren"] or v["siret"][:9]
        xml = facturx_xml.generer(p, v)
        pdf = generate_from_binary(pdf, xml.encode("utf-8"), flavor="factur-x", level="basicwl", check_xsd=True)
        facturx_ok = True
    nom = f"{p['numero']}.pdf"
    chemin = config.DATA / "fichiers" / nom
    chemin.write_bytes(pdf)
    jeton = store.ajouter_fichier(nom, str(chemin), "application/pdf", len(pdf))
    import json
    with store.conn() as c:
        c.execute("INSERT INTO pieces VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (p["numero"], p["type"], p["date"], p["client"]["nom"], p["totaux"]["ht"], p["totaux"]["tva"], p["totaux"]["ttc"],
                   "émis" if p["type"] != "devis" else "envoyé", json.dumps(p, ensure_ascii=False), jeton, p.get("ref"),
                   store.maintenant().isoformat(timespec="seconds")))
    store.journal("piece", {"numero": p["numero"], "ttc": p["totaux"]["ttc"]})
    return {"numero": p["numero"], "type": p["type"], "totaux": p["totaux"], "ventilation": p["ventilation"],
            "echeance": p.get("echeance"), "validite": p.get("validite"),
            "fichier_id": jeton, "url_pdf": f"{config.URL_PUBLIQUE}/f/{jeton}/{nom}", "facturx": facturx_ok,
            "avertissements": avert + ([] if facturx_ok or p["type"] == "devis" else
                                       ["Factur-X non intégré tant que le SIREN du vendeur n'est pas renseigné."])}


def creer_document(titre: str, contenu_markdown: str, sous_titre: str = "", type_doc: str = "document") -> dict:
    corps = md.markdown(contenu_markdown, extensions=["tables", "sane_lists", "nl2br"])
    auj = store.maintenant()
    h = (f"<html><head><meta charset='utf-8'></head><body>{_logo()}"
         f"<div class='small muted' style='margin-bottom:14px'>{html.escape(type_doc.capitalize())} · {auj.strftime('%d/%m/%Y')}</div>"
         f"<h1>{html.escape(titre)}</h1>{('<p class=muted>' + html.escape(sous_titre) + '</p>') if sous_titre else ''}"
         f"<div style='margin-top:10px'>{corps}</div>"
         f"<p class='small muted' style='margin-top:18px'>{html.escape(config.V['nom'])} — {html.escape(config.V['email'])}</p></body></html>")
    pdf = rendre_pdf(h)
    import re
    import unicodedata
    base = unicodedata.normalize("NFKD", titre).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-")[:60] or "document"
    nom = f"{auj.strftime('%Y-%m-%d')}_{base}.pdf"
    chemin = config.DATA / "fichiers" / f"{auj.strftime('%H%M%S%f')}_{nom}"
    chemin.write_bytes(pdf)
    jeton = store.ajouter_fichier(nom, str(chemin), "application/pdf", len(pdf))
    store.journal("document", {"titre": titre})
    return {"fichier_id": jeton, "url_pdf": f"{config.URL_PUBLIQUE}/f/{jeton}/{nom}", "pages_octets": len(pdf)}
