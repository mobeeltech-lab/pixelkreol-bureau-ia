"""
title: Bureau IA — Apprentissage continu
author: PixelKréol
description: Recherche GitHub, création et amélioration de skills, prompts, connaissances et outils, journal de leçons par agent : le Bureau IA s'améliore en continu.
version: 1.1.0
requirements:
"""

import json
import re
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests
from pydantic import BaseModel, Field

REUNION = timezone(timedelta(hours=4))


class Tools:
    class Valves(BaseModel):
        OWUI_BASE_URL: str = Field(
            default="http://localhost:8080",
            description="Adresse interne d'Open WebUI (depuis le conteneur).",
        )
        OWUI_API_KEY: str = Field(
            default="",
            description="Clé API Open WebUI (facultatif : sinon un jeton est généré pour l'utilisateur courant).",
        )
        GITHUB_TOKEN: str = Field(
            default="",
            description="Jeton GitHub (facultatif, relève la limite de 60 à 5000 requêtes/heure).",
        )
        MAX_LESSONS: int = Field(
            default=60,
            description="Nombre maximum de leçons conservées par agent (les plus anciennes sont archivées).",
        )
        AUTORISER_CREATION_OUTILS: bool = Field(
            default=False,
            description="Autoriser l'agent à installer lui-même de nouveaux outils (jamais activés automatiquement). Désactivé par défaut.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------ utils
    def _token(self, __user__: dict) -> str:
        if self.valves.OWUI_API_KEY:
            return self.valves.OWUI_API_KEY
        try:
            from open_webui.utils.auth import create_token

            return create_token(data={"id": __user__["id"]}, expires_delta=timedelta(minutes=10))
        except Exception as e:  # pragma: no cover
            raise RuntimeError(f"Impossible de générer un jeton interne : {e}")

    def _api(self, method: str, path: str, __user__: dict, body: dict = None):
        r = requests.request(
            method,
            self.valves.OWUI_BASE_URL.rstrip("/") + path,
            headers={"Authorization": f"Bearer {self._token(__user__)}", "Content-Type": "application/json"},
            data=json.dumps(body) if body is not None else None,
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    def _gh(self, path: str, params: dict = None):
        h = {"Accept": "application/vnd.github+json", "User-Agent": "pixelkreol-bureau-ia"}
        if self.valves.GITHUB_TOKEN:
            h["Authorization"] = f"Bearer {self.valves.GITHUB_TOKEN}"
        r = requests.get("https://api.github.com" + path, headers=h, params=params, timeout=30)
        if r.status_code == 403 and "rate limit" in r.text.lower():
            raise RuntimeError("Limite GitHub atteinte (60 requêtes/heure sans jeton). Ajouter GITHUB_TOKEN dans les réglages de l'outil.")
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    @staticmethod
    def _slug(text: str) -> str:
        s = text.lower()
        for a, b in (("àâä", "a"), ("éèêë", "e"), ("îï", "i"), ("ôö", "o"), ("ùûü", "u"), ("ç", "c")):
            for ch in a:
                s = s.replace(ch, b)
        return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60] or "skill"

    def _get_skill(self, skill_id: str, __user__: dict):
        try:
            return self._api("GET", f"/api/v1/skills/id/{skill_id}", __user__)
        except RuntimeError:
            return None

    def _save_skill(self, skill: dict, __user__: dict, create: bool):
        body = {
            "id": skill["id"],
            "name": skill["name"],
            "description": skill.get("description", ""),
            "content": skill["content"],
            "meta": skill.get("meta") or {"description": skill.get("description", ""), "tags": ["pixelkreol"]},
            "access_control": None,
            "is_active": True,
        }
        path = "/api/v1/skills/create" if create else f"/api/v1/skills/id/{skill['id']}/update"
        return self._api("POST", path, __user__, body)

    def _attach(self, model_id: str, skill_id: str, __user__: dict) -> str:
        if not model_id:
            return ""
        try:
            m = self._api("GET", f"/api/v1/models/model?id={urllib.parse.quote(model_id)}", __user__)
        except RuntimeError:
            return f" (agent {model_id} introuvable, skill non rattaché)"
        meta = m.get("meta") or {}
        ids = list(meta.get("skillIds") or [])
        if skill_id in ids:
            return ""
        ids.append(skill_id)
        meta["skillIds"] = ids
        body = {k: m.get(k) for k in ("id", "base_model_id", "name", "params", "is_active")}
        body["meta"] = meta
        if "access_grants" in m:
            body["access_grants"] = m.get("access_grants")
        self._api("POST", f"/api/v1/models/model/update?id={urllib.parse.quote(model_id)}", __user__, body)
        return f" et rattaché à l'agent {model_id}"

    # --------------------------------------------------------------- GitHub
    def rechercher_github(self, requete: str, langage: str = "", nombre: int = 8, __user__: dict = {}) -> str:
        """
        Cherche des dépôts GitHub utiles pour améliorer le Bureau IA ou un projet client.
        :param requete: Mots-clés de recherche (ex. "open webui tools", "n8n workflow facture").
        :param langage: Langage de programmation à filtrer (facultatif, ex. "python").
        :param nombre: Nombre de résultats (1 à 20).
        :return: Liste des dépôts avec étoiles, licence, dernière mise à jour et description.
        """
        q = requete + (f" language:{langage}" if langage else "")
        data = self._gh("/search/repositories", {"q": q, "sort": "stars", "order": "desc", "per_page": max(1, min(int(nombre), 20))})
        lignes = []
        for it in data.get("items", []):
            lic = (it.get("license") or {}).get("spdx_id") or "?"
            lignes.append(
                f"- {it['full_name']} — ★{it['stargazers_count']} · licence {lic} · maj {it['pushed_at'][:10]}\n  {it.get('description') or ''}\n  {it['html_url']}"
            )
        return "\n".join(lignes) or "Aucun dépôt trouvé."

    def lire_depot_github(self, depot: str, __user__: dict = {}) -> str:
        """
        Lit la fiche et le README d'un dépôt GitHub pour évaluer s'il peut servir au Bureau IA.
        :param depot: Nom complet du dépôt, au format "proprietaire/nom".
        :return: Métadonnées du dépôt et début du README.
        """
        info = self._gh(f"/repos/{depot}")
        try:
            import base64

            rd = self._gh(f"/repos/{depot}/readme")
            readme = base64.b64decode(rd.get("content", "")).decode("utf-8", "ignore")
        except Exception:
            readme = "(pas de README)"
        lic = (info.get("license") or {}).get("spdx_id") or "?"
        return (
            f"{info['full_name']} — ★{info['stargazers_count']} · licence {lic} · issues ouvertes {info['open_issues_count']} · "
            f"dernier push {info['pushed_at'][:10]}\n{info.get('description') or ''}\n\nREADME (extrait) :\n{readme[:6000]}"
        )

    # --------------------------------------------------------------- skills
    def lister_skills(self, __user__: dict = {}) -> str:
        """
        Liste les skills existants du Bureau IA (identifiant, nom, description).
        :return: La liste des skills.
        """
        items = self._api("GET", "/api/v1/skills/", __user__)
        if isinstance(items, dict):
            items = items.get("items", [])
        return "\n".join(f"- {s['id']} — {s['name']} : {s.get('description','')}" for s in items) or "Aucun skill."

    def lire_skill(self, skill_id: str, __user__: dict = {}) -> str:
        """
        Affiche le contenu complet d'un skill.
        :param skill_id: Identifiant du skill (ex. "devis-pixelkreol").
        :return: Le contenu du skill.
        """
        s = self._get_skill(skill_id, __user__)
        return f"# {s['name']}\n{s.get('content','')}" if s else f"Skill {skill_id} introuvable."

    def creer_skill(self, nom: str, description: str, contenu: str, agent_id: str = "", __user__: dict = {}) -> str:
        """
        Crée un nouveau skill (méthode de travail réutilisable) et peut le rattacher à un agent.
        À utiliser seulement après validation explicite de Will.
        :param nom: Nom lisible du skill (ex. "Devis site e-commerce").
        :param description: Une phrase : quand utiliser ce skill.
        :param contenu: Contenu Markdown : quand l'utiliser, méthode en étapes, format de sortie, garde-fous.
        :param agent_id: Identifiant de l'agent à qui le rattacher (ex. "pk-commercial"), facultatif.
        :return: Confirmation avec l'identifiant créé.
        """
        sid = self._slug(nom)
        if self._get_skill(sid, __user__):
            return f"Le skill {sid} existe déjà : utiliser ameliorer_skill."
        self._save_skill({"id": sid, "name": nom, "description": description, "content": contenu,
                          "meta": {"description": description, "tags": [t for t in ["pixelkreol", agent_id] if t]}}, __user__, create=True)
        return f"Skill « {nom} » créé (id {sid}){self._attach(agent_id, sid, __user__)}."

    def ameliorer_skill(self, skill_id: str, nouveau_contenu: str, raison: str, __user__: dict = {}) -> str:
        """
        Remplace le contenu d'un skill par une version améliorée, en gardant un historique des versions.
        À utiliser seulement après validation explicite de Will.
        :param skill_id: Identifiant du skill à améliorer.
        :param nouveau_contenu: Nouveau contenu Markdown complet du skill.
        :param raison: Ce qui a été amélioré et pourquoi (une phrase).
        :return: Confirmation.
        """
        s = self._get_skill(skill_id, __user__)
        if not s:
            return f"Skill {skill_id} introuvable."
        meta = s.get("meta") or {}
        hist = list(meta.get("historique") or [])
        hist.append({"date": datetime.now(REUNION).strftime("%Y-%m-%d %H:%M"), "raison": raison, "ancien": (s.get("content") or "")[:4000]})
        meta["historique"] = hist[-10:]
        s.update({"content": nouveau_contenu, "meta": meta})
        self._save_skill(s, __user__, create=False)
        return f"Skill {skill_id} amélioré ({raison}). Version précédente conservée dans l'historique."

    # -------------------------------------------------------------- prompts
    def lister_prompts(self, __user__: dict = {}) -> str:
        """
        Liste les prompts (commandes /…) disponibles dans le Bureau IA.
        :return: La liste des commandes et leur nom.
        """
        items = self._api("GET", "/api/v1/prompts/", __user__)
        if isinstance(items, dict):
            items = items.get("items", [])
        return "\n".join(f"- /{p['command']} — {p.get('name','')}" for p in items) or "Aucun prompt."

    def creer_prompt(self, commande: str, nom: str, contenu: str, __user__: dict = {}) -> str:
        """
        Crée un prompt réutilisable (commande /…) pour tous les agents. Les variables s'écrivent {{nom_variable}}.
        À utiliser seulement après validation explicite de Will.
        :param commande: Nom court de la commande sans barre oblique (ex. "devis-express").
        :param nom: Titre lisible (ex. "Devis express").
        :param contenu: Texte du prompt, avec des variables {{...}} pour ce qui change à chaque utilisation.
        :return: Confirmation.
        """
        cmd = self._slug(commande.lstrip("/"))
        items = self._api("GET", "/api/v1/prompts/", __user__)
        if isinstance(items, dict):
            items = items.get("items", [])
        if any(p.get("command") == cmd for p in items):
            return f"La commande /{cmd} existe déjà."
        self._api("POST", "/api/v1/prompts/create", __user__, {"command": cmd, "name": nom, "content": contenu, "access_control": None})
        return f"Prompt /{cmd} « {nom} » créé."

    # -------------------------------------------------------- connaissances
    def lister_connaissances(self, __user__: dict = {}) -> str:
        """
        Liste les bases de connaissances du Bureau IA et leurs fichiers.
        :return: Bases et fichiers.
        """
        data = self._api("GET", "/api/v1/knowledge/", __user__)
        items = data.get("items", data) if isinstance(data, dict) else data
        out = []
        for kb in items:
            files = [f.get("meta", {}).get("name") or f.get("filename", "?") for f in (kb.get("files") or [])]
            out.append(f"- {kb['name']} (id {kb['id']}) : {', '.join(files) or 'aucun fichier listé'}")
        return "\n".join(out) or "Aucune base de connaissances."

    def ajouter_connaissance(self, base: str, titre: str, texte: str, description_base: str = "", __user__: dict = {}) -> str:
        """
        Ajoute un document texte à une base de connaissances (créée si elle n'existe pas) : fiche client, procédure, tarif, veille validée.
        À utiliser seulement après validation explicite de Will.
        :param base: Nom de la base (ex. "PixelKréol — Équipe et procédures").
        :param titre: Titre du document, sert de nom de fichier (ex. "Procédure relance impayés").
        :param texte: Contenu complet du document.
        :param description_base: Description de la base si elle doit être créée.
        :return: Confirmation ou erreur explicite.
        """
        data = self._api("GET", "/api/v1/knowledge/", __user__)
        items = data.get("items", data) if isinstance(data, dict) else data
        kb = next((k for k in items if k.get("name") == base), None)
        if not kb:
            kb = self._api("POST", "/api/v1/knowledge/create", __user__, {"name": base, "description": description_base or base, "access_control": None})
        fname = self._slug(titre) + ".txt"
        r = requests.post(
            self.valves.OWUI_BASE_URL.rstrip("/") + "/api/v1/files/?process=true&process_in_background=false",
            headers={"Authorization": f"Bearer {self._token(__user__)}"},
            files={"file": (fname, texte.encode("utf-8"), "text/plain")},
            timeout=120,
        )
        if r.status_code >= 400:
            return f"Échec de l'envoi du fichier : {r.status_code} {r.text[:200]}"
        fid = r.json()["id"]
        try:
            self._api("POST", f"/api/v1/knowledge/{kb['id']}/file/add", __user__, {"file_id": fid})
        except RuntimeError as e:
            return f"Fichier envoyé mais non indexé dans « {base} » : {e}. Vérifier le réglage Documents (embedding) d'Open WebUI."
        return f"Document « {titre} » ajouté à la base « {base} »."

    # ----------------------------------------------------------------- outils
    def lister_outils(self, __user__: dict = {}) -> str:
        """
        Liste les outils (tools) installés dans le Bureau IA.
        :return: La liste des outils.
        """
        items = self._api("GET", "/api/v1/tools/", __user__)
        if isinstance(items, dict):
            items = items.get("items", [])
        return "\n".join(f"- {t['id']} — {t.get('name','')} : {(t.get('meta') or {}).get('description','')}" for t in items) or "Aucun outil."

    def proposer_outil(self, identifiant: str, nom: str, description: str, code_python: str, __user__: dict = {}) -> str:
        """
        Propose un nouvel outil Open WebUI (code Python avec une classe Tools). Par sécurité, l'outil n'est installé
        que si le réglage AUTORISER_CREATION_OUTILS est activé par Will ; il n'est jamais activé automatiquement sur un agent.
        Sinon, le code est renvoyé pour que Will le relise et l'installe lui-même.
        :param identifiant: Identifiant court (ex. "pk_meteo_marine").
        :param nom: Nom lisible.
        :param description: À quoi sert l'outil.
        :param code_python: Code complet de l'outil (en-tête docstring, classe Tools, méthodes documentées).
        :return: Confirmation ou code à relire.
        """
        if "class Tools" not in code_python:
            return "Le code doit contenir une classe Tools."
        if not self.valves.AUTORISER_CREATION_OUTILS:
            return (
                "Installation automatique désactivée (réglage AUTORISER_CREATION_OUTILS). Code proposé à relire par Will, "
                "à coller dans Espace de travail → Outils → Créer :\n\n" + code_python
            )
        tid = re.sub(r"[^a-z0-9_]", "_", identifiant.lower())[:50]
        self._api("POST", "/api/v1/tools/create", __user__, {"id": tid, "name": nom, "content": code_python,
                                                             "meta": {"description": description, "manifest": {}}, "access_control": None})
        return f"Outil {tid} installé mais NON activé : Will doit relire le code et l'activer sur les agents voulus."

    # -------------------------------------------------------------- leçons
    def enregistrer_lecon(self, agent_id: str, lecon: str, categorie: str = "méthode", __user__: dict = {}) -> str:
        """
        Enregistre une leçon apprise dans le journal d'apprentissage d'un agent (correction de Will,
        erreur à ne plus faire, préférence client, meilleure méthode). Le journal est un skill rattaché
        à l'agent, donc la leçon est appliquée dans toutes ses conversations suivantes.
        :param agent_id: Identifiant de l'agent concerné (ex. "pk-commercial").
        :param lecon: La leçon, formulée comme une règle d'action courte (ex. "Toujours proposer 3 créneaux").
        :param categorie: méthode, préférence, erreur, client ou outil.
        :return: Confirmation.
        """
        sid = f"journal-{self._slug(agent_id.replace('pk-', ''))}"
        s = self._get_skill(sid, __user__)
        date = datetime.now(REUNION).strftime("%Y-%m-%d")
        ligne = f"- [{date}] ({categorie}) {lecon.strip()}"
        if not s:
            entete = (
                f"# Journal d'apprentissage — {agent_id}\n\n"
                "Ces règles viennent des retours de Will et de l'expérience des missions précédentes. "
                "Elles priment sur les habitudes par défaut. En cas de contradiction, la leçon la plus récente l'emporte.\n\n"
                "## Leçons\n"
            )
            self._save_skill({"id": sid, "name": f"Journal d'apprentissage — {agent_id}",
                              "description": f"Leçons apprises par {agent_id}, appliquées à chaque mission",
                              "content": entete + ligne + "\n", "meta": {"tags": ["pixelkreol", "apprentissage", agent_id]}}, __user__, create=True)
            return f"Journal créé pour {agent_id} avec cette première leçon{self._attach(agent_id, sid, __user__)}."
        contenu = s.get("content") or ""
        if lecon.strip().lower() in contenu.lower():
            return "Cette leçon est déjà dans le journal."
        tete, _, corps = contenu.partition("## Leçons\n")
        lignes = [l for l in corps.splitlines() if l.strip()] + [ligne]
        meta = s.get("meta") or {}
        if len(lignes) > self.valves.MAX_LESSONS:
            archive = list(meta.get("archive") or []) + lignes[: len(lignes) - self.valves.MAX_LESSONS]
            meta["archive"] = archive[-500:]
            lignes = lignes[-self.valves.MAX_LESSONS:]
        s.update({"content": tete + "## Leçons\n" + "\n".join(lignes) + "\n", "meta": meta})
        self._save_skill(s, __user__, create=False)
        self._attach(agent_id, sid, __user__)
        return f"Leçon enregistrée pour {agent_id} ({len(lignes)} leçons actives)."

    def lire_journal(self, agent_id: str, __user__: dict = {}) -> str:
        """
        Affiche le journal d'apprentissage d'un agent.
        :param agent_id: Identifiant de l'agent (ex. "pk-commercial").
        :return: Les leçons enregistrées.
        """
        sid = f"journal-{self._slug(agent_id.replace('pk-', ''))}"
        s = self._get_skill(sid, __user__)
        return s.get("content", "") if s else f"Aucun journal pour {agent_id} pour l'instant."
