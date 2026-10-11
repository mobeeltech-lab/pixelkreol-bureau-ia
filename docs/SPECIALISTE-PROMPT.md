# Spécialiste Prompt — mode d'emploi et installation

Agent n° 34 du Bureau IA PixelKréol (`pk-34-specialiste-prompt`) et n° 10 de l'instance ABI Holding (`pk-10-specialiste-prompt`).
Il transforme une demande brouillon en **demande claire adressée au bon agent**, sans jamais répondre lui-même à la question métier.

## Comment s'en servir
Dans le chat, choisir `pk-34-specialiste-prompt` (ou écrire « améliore ma demande : … » avec `pk-auto`, qui l'aiguille tout seul).

| Je veux | J'écris | Il répond |
|---|---|---|
| Rendre ma demande claire | « Améliore ma demande : fais un truc pour le site du coiffeur » | au plus 3 questions courtes avec une réponse par défaut (répondre « ok » pour tout accepter), puis : 1) l'agent recommandé (nom exact `pk-NN-id`, ou `pk-equipe` si plusieurs métiers) et pourquoi ; 2) la demande réécrite dans un bloc à copier, avec les 5 ingrédients ; 3) « Ce que j'ai ajouté » |
| Améliorer la fiche d'un agent | « Améliore la fiche de l'agent Relances » + coller la fiche | la fiche corrigée, la liste des changements avec raisons, 3 questions de test, et « Feu : ORANGE — à valider par Will » |
| Avoir des modèles | « Modèles de demandes » | 5 à 10 modèles à compléter, chacun avec son agent |

Les 5 ingrédients : **contexte** (qui, pour qui) · **tâche** (verbe d'action) · **matière** (pièces, chiffres, documents à coller) · **forme du résultat** · **limites** (interdits, feu attendu, échéance).
Une donnée manquante reste entre crochets (`[montant HT]`) : il n'invente ni prix, ni nom, ni date. Un mot de passe ou une clé collés dans la demande sont remplacés par `[SECRET RETIRÉ]`.
Pour **créer un nouvel agent**, c'est la Fabrique d'agents (`pk-25-fabrique-agents`) : le Spécialiste Prompt améliore l'existant, il ne crée rien dans le registre.

## Où est la liste des agents ?
Nulle part à recopier. Le champ `inclure_annuaire: true` de sa fiche demande à l'orchestrateur d'ajouter, **à chaque requête et pour cet agent seulement**, la liste
« `pk-NN-id` : rôle » construite à partir du registre chargé (`registre.yaml` **et** `perso.yaml`). Un agent ajouté ou retiré apparaît donc tout seul.
La liste est bornée : 110 caractères par rôle, 6 000 caractères au total (au-delà, une ligne « liste tronquée » est ajoutée). Aucun autre agent ne reçoit cette liste.

## Modèles de demandes
`docs/modeles-de-demandes.txt` (15 modèles PixelKréol, nom exact des agents, aucun prix). Pour les enregistrer dans Open WebUI : **Espace de travail > Prompts > « + »**,
titre, commande (par exemple `/devis-vitrine`) et contenu. Aucun fichier d'import n'est fourni : le format d'import de la version 0.12.0 n'a pas pu être vérifié (documentation inaccessible depuis l'environnement de développement).
Pour ABI : `instances/abi/modeles-de-demandes.txt` (10 modèles).

## Installation sur le serveur PixelKréol
Le code de l'orchestrateur change (`orchestrateur/app.py` : champ `inclure_annuaire` et génération de la liste), donc **l'image doit être reconstruite** ; la fiche seule ne suffirait pas.

Fichiers modifiés : `agents/registre.yaml` (nouvel agent n° 34), `orchestrateur/app.py`. Fichiers du banc (facultatifs pour les agents) : `scripts/banc/*` (voir `scripts/banc/DEPLOIEMENT.md`).

1. Copier les fichiers *(terminal Hostinger)* : votre script habituel `/root/maj-bureau/remise/maj-bureau.sh` copie déjà `registre.yaml` et les fichiers de l'orchestrateur ; vérifier qu'il a bien posé la nouvelle version :
```bash
grep -c "inclure_annuaire" /opt/pixelkreol-bureau-ia/orchestrateur/app.py /opt/pixelkreol-bureau-ia/agents/registre.yaml
```
Les deux nombres doivent être supérieurs à 0.

2. Reconstruire et relancer l'orchestrateur *(terminal Hostinger)* :
```bash
cd /opt/pixelkreol-bureau-ia && docker compose up -d --build pk_orchestrateur
```
3. Vérifier *(terminal Hostinger)* :
```bash
docker exec pk_orchestrateur python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:4001/health').read().decode())"
```
La réponse doit contenir `"agents":34`. Si elle affiche 33, regarder les avertissements : `docker logs --tail 30 pk_orchestrateur`. Une cause possible : un agent de `agents/perso.yaml` utilise déjà le n° 34 ou l'id `specialiste-prompt` (l'agent en double est alors ignoré, rien d'autre ne casse).

4. Essayer : dans le chat, `pk-34-specialiste-prompt`, « Améliore ma demande : fais un devis pour le salon Chez Fictif ». Il doit citer `pk-04-devis`.

**Retour arrière** *(terminal Hostinger)* : remettre l'ancien `registre.yaml` et l'ancien `app.py` (sauvegardes faites par `maj-bureau.sh` ou copie de `/root/registre.yaml.avant-lot1`), puis `docker compose up -d --build pk_orchestrateur`. Aucune donnée n'est touchée.

## Installation sur l'instance ABI
*(terminal Hostinger)*
```bash
cd /opt/abi-bureau-ia && git pull
cd instances/abi && dc up -d --build abi_orchestrateur
docker exec abi_orchestrateur python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:4001/health').read().decode())"
```
La réponse doit contenir `"agents":10`. (`dc` est le raccourci décrit dans `instances/abi/LISEZMOI.md`.) Le registre d'ABI est monté en lecture seule : la nouvelle fiche est prise au `git pull`, l'image est reconstruite à cause du nouveau code.

## Limites connues
- Les réponses de l'agent dépendent du modèle `pk-costaud` : les règles automatiques du banc vérifient la forme (agent existant, bloc de copie, 5 ingrédients, ≤ 3 questions, crochets, secret retiré), pas la **pertinence** de la demande réécrite.
- La liste des agents ajoute environ 3 700 caractères au message système de cet agent chez PixelKréol (1 300 chez ABI) : sans effet sur les autres agents.
- « Améliore ma demande : relis le bail » chez ABI part chez le Juriste : la règle prioritaire du Juriste (bail + relecture, numéro plus bas) passe avant celle du Spécialiste Prompt.
- Le routage `pk-auto` par mots-clés peut hésiter sur une demande qui contient à la fois la formule du Spécialiste Prompt et un mot-clé fort d'un autre métier : la formule explicite (« améliore ma demande », « écris le prompt »…) l'emporte, sauf cas particulier ci-dessus.
