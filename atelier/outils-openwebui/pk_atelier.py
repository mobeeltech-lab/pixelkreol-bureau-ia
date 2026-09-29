"""
title: PK Atelier — factures, PDF, mails, sites et web
author: PixelKréol
description: Donne aux agents du Bureau IA de vraies mains : factures/devis/avoirs PDF (Factur-X), documents PDF dans la charte, préparation et envoi de mails (avec validation de Will), génération et hébergement autonome de sites (lien démo, sous-domaine HTTPS, domaine du client après validation de Will), export zip. Accès au web : recherche privée, lecture de pages, recherche approfondie sourcée, audit de site prospect, registre officiel des entreprises.
version: 1.2.0
"""

import json
import re

import requests
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        ATELIER_URL: str = Field(default="http://pk_atelier:8000", description="Adresse interne du service PK Atelier.")
        ATELIER_KEY: str = Field(default="", description="Clé API de PK Atelier (affichée à la fin de l'installation).")

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------ utilitaires
    def _call(self, method: str, path: str, body=None, params=None):
        if not self.valves.ATELIER_KEY:
            return {"erreur": "Clé ATELIER_KEY non renseignée dans les réglages de l'outil PK Atelier."}
        try:
            r = requests.request(method, self.valves.ATELIER_URL.rstrip("/") + path, json=body, params=params,
                                 headers={"Authorization": f"Bearer {self.valves.ATELIER_KEY}"}, timeout=120)
        except requests.RequestException as ex:
            return {"erreur": f"PK Atelier injoignable : {ex}"}
        try:
            data = r.json()
        except ValueError:
            data = {"reponse": r.text[:500]}
        if r.status_code >= 400:
            return {"erreur": data.get("detail", data) if isinstance(data, dict) else data, "code": r.status_code}
        return data

    @staticmethod
    def _nombre(x: str) -> float:
        x = str(x).replace("€", "").replace(" ", "").replace("\xa0", "").replace(" ", "").replace(",", ".")
        return float(x or 0)

    def _lignes(self, texte: str) -> list:
        """Une ligne par prestation : « désignation ; quantité ; prix unitaire HT ; taux TVA (facultatif) »."""
        out = []
        for l in (texte or "").strip().splitlines():
            parts = [p.strip() for p in re.split(r"[;|]", l)]
            if len(parts) < 2 or not parts[0]:
                continue
            if len(parts) == 2:
                parts = [parts[0], "1", parts[1]]
            ligne = {"designation": parts[0], "quantite": self._nombre(parts[1] or 1), "prix_unitaire_ht": self._nombre(parts[2])}
            if len(parts) > 3 and parts[3]:
                ligne["tva"] = self._nombre(parts[3].replace("%", ""))
            out.append(ligne)
        return out

    @staticmethod
    def _agent(m) -> str:
        return (m or {}).get("name", "") if isinstance(m, dict) else ""

    def _fmt_piece(self, r: dict) -> str:
        if "erreur" in r:
            return f"❌ {r['erreur']}"
        t = r["totaux"]
        lignes = [f"✅ {r['type'].capitalize()} {r['numero']} créé(e).",
                  f"Total HT {t['ht']:.2f} € · TVA {t['tva']:.2f} € · TTC {t['ttc']:.2f} €" + (f" · net à payer {t['a_payer']:.2f} €" if t.get("acompte") else ""),
                  f"PDF : {r['url_pdf']}",
                  f"Identifiant de fichier (pour une pièce jointe) : {r['fichier_id']}"]
        if r.get("facturx"):
            lignes.append("Factur-X (facture électronique) intégré.")
        for a in r.get("avertissements") or []:
            lignes.append(f"⚠️ {a}")
        return "\n".join(lignes)

    # ------------------------------------------------------------ pièces
    def creer_facture(self, client_nom: str, lignes: str, client_adresse: str = "", client_email: str = "", client_siren: str = "",
                      objet: str = "", acompte_deja_verse: float = 0, echeance_jours: int = 30, notes: str = "") -> str:
        """
        Crée une FACTURE PDF conforme (numéro continu, mentions obligatoires, TVA Réunion 8,5 % par défaut, Factur-X) et renvoie son lien.
        :param client_nom: Nom ou raison sociale du client.
        :param lignes: Une prestation par ligne : « désignation ; quantité ; prix unitaire HT ; TVA % (facultatif) ». Ex. « Site vitrine ; 1 ; 1200 ».
        :param client_adresse: Adresse postale du client (retours à la ligne autorisés).
        :param client_email: E-mail du client.
        :param client_siren: SIREN du client s'il est professionnel.
        :param objet: Objet de la facture.
        :param acompte_deja_verse: Montant TTC d'acompte déjà encaissé à déduire.
        :param echeance_jours: Délai de paiement en jours (60 maximum).
        :param notes: Mention libre en bas de facture.
        :return: Numéro, totaux, lien du PDF et identifiant de fichier.
        """
        return self._fmt_piece(self._call("POST", "/api/pieces", {
            "type": "facture", "client": {"nom": client_nom, "adresse": client_adresse, "email": client_email, "siren": client_siren},
            "lignes": self._lignes(lignes), "objet": objet, "acompte_deja_verse": acompte_deja_verse, "echeance_jours": echeance_jours, "notes": notes}))

    def creer_devis(self, client_nom: str, lignes: str, client_adresse: str = "", client_email: str = "", objet: str = "",
                    validite_jours: int = 30, kap_numerik: str = "", conditions: str = "", notes: str = "") -> str:
        """
        Crée un DEVIS PDF numéroté dans la charte PixelKréol, avec zone « Bon pour accord ».
        :param client_nom: Nom du client.
        :param lignes: Une prestation par ligne : « désignation ; quantité ; prix unitaire HT ; TVA % (facultatif) ».
        :param client_adresse: Adresse du client.
        :param client_email: E-mail du client.
        :param objet: Objet du devis.
        :param validite_jours: Durée de validité en jours.
        :param kap_numerik: Phrase d'estimation Kap Numérik à afficher (ex. « aide estimée 3 200 €, reste à charge 2 300 € HT »), facultatif.
        :param conditions: Conditions de paiement (par défaut : acompte 30 % à la commande, solde à la livraison).
        :param notes: Mention libre.
        :return: Numéro, totaux et lien du PDF.
        """
        return self._fmt_piece(self._call("POST", "/api/pieces", {
            "type": "devis", "client": {"nom": client_nom, "adresse": client_adresse, "email": client_email},
            "lignes": self._lignes(lignes), "objet": objet, "validite_jours": validite_jours, "kap": kap_numerik,
            "conditions": conditions, "notes": notes}))

    def transformer_devis_en_facture(self, numero_devis: str, acompte_deja_verse: float = 0) -> str:
        """
        Transforme un devis accepté en facture (mêmes lignes, référence au devis).
        :param numero_devis: Numéro du devis, ex. « D-2026-0004 ».
        :param acompte_deja_verse: Acompte TTC déjà encaissé à déduire.
        :return: La facture créée.
        """
        return self._fmt_piece(self._call("POST", f"/api/pieces/{numero_devis}/facturer", params={"acompte_deja_verse": acompte_deja_verse}))

    def creer_avoir(self, numero_facture: str, client_nom: str, lignes: str, motif: str = "") -> str:
        """
        Crée un AVOIR (facture d'annulation ou de remise) lié à une facture. Une facture émise ne se supprime jamais : on fait un avoir.
        :param numero_facture: Facture concernée, ex. « F-2026-0007 ».
        :param client_nom: Nom du client.
        :param lignes: Lignes à annuler : « désignation ; quantité ; prix unitaire HT » (montants positifs, le signe est géré).
        :param motif: Raison de l'avoir.
        :return: L'avoir créé.
        """
        return self._fmt_piece(self._call("POST", "/api/pieces", {"type": "avoir", "ref": numero_facture, "client": {"nom": client_nom},
                                                                  "lignes": self._lignes(lignes), "notes": motif}))

    def marquer_facture_payee(self, numero_facture: str) -> str:
        """
        Enregistre qu'une facture a été payée.
        :param numero_facture: Numéro de la facture.
        :return: Confirmation.
        """
        r = self._call("POST", f"/api/pieces/{numero_facture}/payee")
        return f"❌ {r['erreur']}" if "erreur" in r else f"✅ {numero_facture} marquée payée."

    def lister_factures(self, type_piece: str = "", statut: str = "") -> str:
        """
        Liste le registre des pièces (factures, devis, avoirs) avec montants, statuts et liens.
        :param type_piece: facture, devis ou avoir (vide = tout).
        :param statut: émis, payé, envoyé, accepté (vide = tout).
        :return: Le registre.
        """
        r = self._call("GET", "/api/pieces", params={"type": type_piece, "statut": statut})
        if isinstance(r, dict) and "erreur" in r:
            return f"❌ {r['erreur']}"
        return "\n".join(f"- {p['numero']} · {p['date']} · {p['client']} · {p['total_ttc']:.2f} € TTC · {p['statut']} · {p['url_pdf']}" for p in r) or "Registre vide."

    # ------------------------------------------------------------ documents
    def creer_pdf(self, titre: str, contenu_markdown: str, sous_titre: str = "", type_document: str = "document") -> str:
        """
        Transforme un texte en PDF propre dans la charte PixelKréol (proposition commerciale, compte rendu, rapport, guide, courrier…).
        :param titre: Titre du document.
        :param contenu_markdown: Contenu en Markdown (titres ##, listes, tableaux, gras).
        :param sous_titre: Sous-titre facultatif.
        :param type_document: Étiquette affichée (proposition, compte rendu, rapport, guide, courrier…).
        :return: Lien du PDF et identifiant de fichier.
        """
        r = self._call("POST", "/api/pdf", {"titre": titre, "contenu": contenu_markdown, "sous_titre": sous_titre, "type": type_document})
        return f"❌ {r['erreur']}" if "erreur" in r else f"✅ PDF créé : {r['url_pdf']}\nIdentifiant de fichier : {r['fichier_id']}"

    # ------------------------------------------------------------ mails
    def preparer_mail(self, destinataire: str, sujet: str, message: str, pieces_jointes: str = "", copie: str = "", __model__: dict = None) -> str:
        """
        Prépare un e-mail (avec pièces jointes : factures, devis, PDF). Selon la configuration, il part après validation de Will
        (il reçoit un lien « OUI, envoyer ») ou directement pour les adresses de confiance.
        :param destinataire: Adresse(s) e-mail, séparées par des virgules.
        :param sujet: Objet du mail.
        :param message: Corps du mail (Markdown simple autorisé), vouvoiement, signé PixelKréol.
        :param pieces_jointes: Liens des PDF ou identifiants de fichier, séparés par des virgules.
        :param copie: Adresses en copie, facultatif.
        :return: Statut (à valider / envoyé) et lien de validation.
        """
        pj = [p.strip() for p in (pieces_jointes or "").split(",") if p.strip()]
        r = self._call("POST", "/api/mails", {"a": destinataire, "sujet": sujet, "corps": message, "cc": copie,
                                              "pieces_jointes": pj, "agent": self._agent(__model__)})
        if "erreur" in r:
            return f"❌ {r['erreur']}"
        if r.get("statut") == "envoyé":
            return f"✅ Mail envoyé à {destinataire} (adresse de confiance)."
        msg = f"📨 Mail prêt, en attente de ta validation (feu ROUGE). Lien : {r['lien_validation']}"
        if r.get("notification"):
            msg += "\nUne demande de validation a été envoyée par e-mail à Will."
        return msg

    def suivre_mails(self) -> str:
        """
        Affiche les derniers mails préparés et leur statut (à valider, envoyé, annulé, erreur).
        :return: La liste des mails.
        """
        r = self._call("GET", "/api/mails")
        if isinstance(r, dict) and "erreur" in r:
            return f"❌ {r['erreur']}"
        return "\n".join(f"- {m['cree_le'][:16]} · {m['statut']} · {m['a']} · {m['sujet']}" + (f" · valider : {m['lien_validation']}" if m.get("lien_validation") else "")
                         + (f" · erreur : {m['erreur']}" if m.get("erreur") else "") for m in r) or "Aucun mail."

    # ------------------------------------------------------------ sites
    def generer_site_demo(self, entreprise: str, activite: str, ville: str = "", slogan: str = "", description: str = "",
                          services: str = "", atouts: str = "", telephone: str = "", email: str = "", adresse: str = "",
                          horaires: str = "", zone: str = "", style: str = "lagon", images: str = "", jours: int = 30,
                          mot_de_passe: str = "", sous_domaine: str = "") -> str:
        """
        Génère un site vitrine complet (responsive, SEO local, boutons appel et WhatsApp) et le met en ligne sur un lien de démo à envoyer au client.
        :param entreprise: Nom de l'entreprise.
        :param activite: Métier (ex. « Plombier chauffagiste »).
        :param ville: Commune (ex. « Saint-Pierre »).
        :param slogan: Phrase d'accroche.
        :param description: 1 à 3 phrases de présentation.
        :param services: Un service par ligne : « Titre : description ».
        :param atouts: Un point fort par ligne.
        :param telephone: Téléphone.
        :param email: E-mail.
        :param adresse: Adresse.
        :param horaires: Horaires d'ouverture.
        :param zone: Zone d'intervention.
        :param style: lagon, volcan, vegetal, corail, nuit ou sobre.
        :param images: Liens d'images (https) séparés par des virgules, facultatif.
        :param jours: Durée de mise en ligne de la démo (0 = sans expiration).
        :param mot_de_passe: Mot de passe pour protéger l'aperçu, facultatif.
        :param sous_domaine: Adresse souhaitée (ex. « garage-du-lagon » → garage-du-lagon.demo.mebeeltech.online). Par défaut : le nom de l'entreprise.
        :return: Lien de la démo.
        """
        r = self._call("POST", "/api/sites/generer", {"sous_domaine": sous_domaine,
            "entreprise": entreprise, "activite": activite, "ville": ville, "slogan": slogan, "description": description,
            "services": services, "atouts": atouts, "telephone": telephone, "email": email, "adresse": adresse,
            "horaires": horaires, "zone": zone, "style": style,
            "images": [u.strip() for u in (images or "").split(",") if u.strip()], "jours": jours, "mot_de_passe": mot_de_passe})
        if "erreur" in r:
            return f"❌ {r['erreur']}"
        return self._fmt_site(r, "Démo en ligne")

    @staticmethod
    def _fmt_site(r: dict, titre: str) -> str:
        l = [f"✅ {titre} : {r['url']}"]
        if r.get("url_sous_domaine") and r["url_sous_domaine"] != r["url"]:
            l.append(f"Adresse propre : {r['url_sous_domaine']}" + ("" if r.get("sous_domaine_https") else " (HTTPS en cours d'activation, ~1-2 min : vérifier avec etat_hebergement)"))
        if r.get("url_immediate") and r["url_immediate"] != r["url"]:
            l.append(f"Lien de secours immédiat : {r['url_immediate']}")
        l.append(f"Expire le : {r.get('expire_le') or 'jamais'}{' · protégée par mot de passe' if r.get('protege') else ''}")
        l.append(f"Identifiant : {r['slug']}")
        return "\n".join(l)

    def publier_site(self, nom: str, html: str, jours: int = 30, mot_de_passe: str = "", sous_domaine: str = "") -> str:
        """
        Met en ligne un site écrit en HTML (fichier unique, CSS et JS inclus) sur un lien de démo.
        :param nom: Nom du projet (sert à construire le lien).
        :param html: Code HTML complet de la page.
        :param jours: Durée de mise en ligne (0 = sans expiration).
        :param mot_de_passe: Mot de passe facultatif.
        :param sous_domaine: Adresse souhaitée (ex. « snack-ti-kaz »), facultatif.
        :return: Lien de la démo.
        """
        r = self._call("POST", "/api/sites/publier", {"nom": nom, "html": html, "jours": jours, "mot_de_passe": mot_de_passe, "sous_domaine": sous_domaine})
        if "erreur" in r:
            return f"❌ {r['erreur']}"
        return self._fmt_site(r, "Site en ligne")

    def lister_sites(self) -> str:
        """
        Liste les sites démo en ligne avec leur lien et leur date d'expiration.
        :return: La liste.
        """
        r = self._call("GET", "/api/sites")
        if isinstance(r, dict) and "erreur" in r:
            return f"❌ {r['erreur']}"
        return "\n".join(f"- {s['nom']} ({s['slug']}) · {s['url']} · {'🟢 production' + (' sur ' + s['domaine'] if s.get('domaine') else '') if s.get('production') else 'démo, expire ' + (s['expire_le'] or 'jamais')}{' · 🔒' if s['protege'] else ''}" for s in r) or "Aucun site."

    def prolonger_site(self, identifiant: str, jours: int = 30) -> str:
        """
        Prolonge la mise en ligne d'une démo.
        :param identifiant: Identifiant du site (slug).
        :param jours: Nombre de jours à partir d'aujourd'hui (0 = sans expiration).
        :return: Nouvelle date d'expiration.
        """
        r = self._call("POST", f"/api/sites/{identifiant}/prolonger", params={"jours": jours})
        return f"❌ {r['erreur']}" if "erreur" in r else f"✅ {identifiant} : expire le {r['expire_le'] or 'jamais'}."

    def supprimer_site(self, identifiant: str) -> str:
        """
        Supprime une démo en ligne (feu ROUGE : seulement après le OUI de Will).
        :param identifiant: Identifiant du site (slug).
        :return: Confirmation.
        """
        r = self._call("DELETE", f"/api/sites/{identifiant}")
        if "erreur" in r:
            return f"❌ {r['erreur']}"
        if r.get("validation_requise"):
            return f"⏳ Site en production : Will doit confirmer la suppression ici → {r['lien_validation']}"
        return "✅ Supprimé." if r.get("supprime") else "Site introuvable."

    def mettre_en_production(self, identifiant: str, domaine: str = "") -> str:
        """
        Met un site en ligne pour de bon (sans bandeau « maquette », indexable par Google, sans expiration), sur le domaine du client si fourni.
        Feu ROUGE : crée un lien de validation ; rien ne change tant que Will n'a pas cliqué « OUI ».
        :param identifiant: Identifiant du site (slug).
        :param domaine: Domaine du client (ex. « garagedulagon.re »), facultatif.
        :return: Lien de validation pour Will et consignes DNS à transmettre au client.
        """
        r = self._call("POST", f"/api/sites/{identifiant}/production", {"domaine": domaine})
        if "erreur" in r:
            return f"❌ {r['erreur']}"
        l = [f"⏳ Mise en production demandée pour {identifiant}{' sur ' + r['domaine'] if r.get('domaine') else ''}.",
             f"Will valide ici : {r['lien_validation']}" + (" (lien aussi envoyé par mail)" if r.get("notification") else "")]
        if r.get("domaine"):
            l.append("DNS déjà correct ✅" if r.get("dns_ok") else f"À faire par le client ou son prestataire : {r['instructions_dns']}")
            l.append("Ensuite tout est automatique : branchement nginx et certificat HTTPS (vérifier avec etat_hebergement).")
        return "\n".join(l)

    def etat_hebergement(self, identifiant: str) -> str:
        """
        Donne l'état d'hébergement d'un site : adresses, HTTPS, DNS du domaine client, demande en attente.
        :param identifiant: Identifiant du site (slug).
        :return: L'état détaillé.
        """
        r = self._call("GET", f"/api/sites/{identifiant}/hebergement")
        if "erreur" in r:
            return f"❌ {r['erreur']}"
        l = [f"{r['nom']} ({identifiant}) — {'🟢 production' if r['production'] else 'démo'} · lien à donner : {r['url']}"]
        if r.get("url_sous_domaine"):
            l.append(f"Sous-domaine : {r['url_sous_domaine']} · HTTPS {'✅' if r.get('sous_domaine_https') else '⏳'}")
        if r.get("domaine"):
            l.append(f"Domaine client : {r['domaine']} · état {r.get('domaine_etat')} · DNS {'✅' if r.get('dns_ok') else '❌ ' + ', '.join(r.get('dns_ips') or ['aucune IP'])}")
            if r.get("domaine_message"):
                l.append(f"Détail : {r['domaine_message']}")
            if r.get("instructions_dns"):
                l.append(f"Consigne DNS : {r['instructions_dns']}")
        if r.get("demande_en_attente"):
            l.append(f"⏳ En attente du OUI de Will : {r['demande_en_attente']['action']}")
        if not r.get("hebergement_actif"):
            l.append("ℹ️ Hébergement autonome pas encore installé sur le serveur : seul le lien immédiat fonctionne.")
        return "\n".join(l)

    def exporter_site(self, identifiant: str) -> str:
        """
        Crée un zip des fichiers du site (version propre, sans bandeau) à remettre au client ou à héberger ailleurs.
        :param identifiant: Identifiant du site (slug).
        :return: Lien de téléchargement du zip (utilisable en pièce jointe de preparer_mail).
        """
        r = self._call("POST", f"/api/sites/{identifiant}/exporter")
        return f"❌ {r['erreur']}" if "erreur" in r else f"✅ Zip prêt : {r['url_zip']} ({round(r['octets']/1024)} Ko)"

    # ------------------------------------------------------------ web
    def rechercher_web(self, requete: str, nombre: int = 8) -> str:
        """
        Cherche sur Internet (moteur privé du serveur) : actualités, concurrents, prix, fournisseurs, documentation, aides publiques.
        Toujours citer les liens des sources dans la réponse.
        :param requete: La recherche, précise (ex. « aide Kap Numérik 2026 Région Réunion conditions »).
        :param nombre: Nombre de résultats (1 à 20).
        :return: Titres, liens et extraits.
        """
        r = self._call("POST", "/api/web/chercher", {"requete": requete, "nombre": nombre})
        if "erreur" in r:
            return f"❌ {r['erreur']}"
        if not r.get("resultats"):
            return "Aucun résultat. Détail : " + " ; ".join(r.get("erreurs", [])) + ". Reformuler la recherche ou réessayer."
        return f"Résultats ({r['moteur']}) :\n" + "\n".join(f"- {x['titre']}\n  {x['url']}\n  {x['extrait'][:300]}" for x in r["resultats"])

    def lire_page(self, url: str, max_caracteres: int = 12000) -> str:
        """
        Ouvre une page web et renvoie son titre, sa description et son texte (site d'un client, d'un concurrent, article, documentation, conditions d'une aide).
        :param url: Adresse de la page.
        :param max_caracteres: Longueur maximale du texte (500 à 40000).
        :return: Le contenu de la page et ses principaux liens.
        """
        r = self._call("POST", "/api/web/lire", {"url": url, "max_caracteres": max_caracteres})
        if "erreur" in r:
            return f"❌ {r['erreur']}"
        liens = "\n".join(r.get("liens", [])[:15])
        return (f"URL : {r['url']} (HTTP {r['statut']})\nTitre : {r.get('titre','')}\nDescription : {r.get('description') or '(aucune)'}\n\n"
                f"{r.get('texte','')}" + ("\n[… texte tronqué]" if r.get("tronque") else "") + (f"\n\nLiens de la page :\n{liens}" if liens else ""))

    def recherche_approfondie(self, question: str, pages: int = 3) -> str:
        """
        Répond à une question avec des sources : cherche sur le web puis lit les meilleures pages. À utiliser pour la veille,
        les questions réglementaires, fiscales, les aides, les comparatifs. Synthétiser ensuite en citant chaque source.
        :param question: La question complète.
        :param pages: Nombre de pages à lire (1 à 5).
        :return: Extraits des pages lues avec leurs liens.
        """
        r = self._call("POST", "/api/web/approfondir", {"question": question, "pages": pages})
        if "erreur" in r:
            return f"❌ {r['erreur']}"
        if not r.get("sources"):
            return "Aucune page lisible trouvée. Essayer rechercher_web avec une autre formulation."
        blocs = [f"### Source {i}: {s['titre']}\n{s['url']}\n{s['texte']}" for i, s in enumerate(r["sources"], 1)]
        autres = "\n".join(f"- {x['titre']} : {x['url']}" for x in r.get("autres_resultats", []))
        return "\n\n".join(blocs) + (f"\n\nAutres pistes :\n{autres}" if autres else "") + "\n\nConsigne : synthétiser en citant les sources [1], [2]…"

    def auditer_site(self, url: str) -> str:
        """
        Audite le site d'un prospect ou d'un client (HTTPS, vitesse, mobile, Google, partage réseaux, bouton appel/WhatsApp, formulaire,
        mentions légales) avec une note sur 100 et les corrections à proposer. Idéal pour préparer un rendez-vous commercial.
        :param url: Adresse du site.
        :return: Note, points forts, points faibles et conseils.
        """
        r = self._call("POST", "/api/web/auditer", {"url": url})
        if "erreur" in r:
            return f"❌ {r['erreur']}"
        l = [f"Audit de {r['url']} : {r['note']}/100 ({r['reussis']}/{r['total']} contrôles réussis, réponse en {r['duree_s']} s)"]
        l += [f"{'✅' if c['ok'] else '❌'} {c['controle']}" + ("" if c["ok"] else f" — {c['conseil']}") for c in r["controles"]]
        if r.get("generateur"):
            l.append(f"Outil de création détecté : {r['generateur']}")
        if r.get("titres"):
            l.append("Titres : " + " | ".join(f"{t[0].upper()}: {t[1][:50]}" for t in r["titres"]))
        return "\n".join(l)

    def rechercher_entreprises(self, requete: str = "", code_postal: str = "", departement: str = "974", code_naf: str = "", nombre: int = 10) -> str:
        """
        Cherche des entreprises actives dans le registre officiel (API publique de l'État) : pour vérifier un client (SIREN, adresse)
        ou trouver des prospects par activité et commune.
        :param requete: Nom ou activité (ex. « boulangerie », « Garage du Lagon »).
        :param code_postal: Code postal (ex. « 97460 »), facultatif.
        :param departement: Département (974 par défaut ; vide pour toute la France).
        :param code_naf: Code d'activité NAF (ex. « 10.71C » boulangerie), facultatif.
        :param nombre: Nombre de résultats (1 à 25).
        :return: Liste des entreprises (nom, SIREN, adresse, activité, taille, date de création).
        """
        r = self._call("POST", "/api/web/entreprises", {"requete": requete, "code_postal": code_postal, "departement": departement,
                                                        "naf": code_naf, "nombre": nombre})
        if "erreur" in r:
            return f"❌ {r['erreur']}"
        if not r.get("entreprises"):
            return "Aucune entreprise trouvée."
        return f"{r['total']} entreprise(s) au total, {len(r['entreprises'])} affichée(s) :\n" + "\n".join(
            f"- {e['nom']} · SIREN {e['siren']} · {e.get('adresse') or ''} · NAF {e.get('naf')} · créée {e.get('creation') or '?'}"
            for e in r["entreprises"])
