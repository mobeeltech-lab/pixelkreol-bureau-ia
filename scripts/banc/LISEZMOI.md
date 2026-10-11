# Banc d'essai des agents du Bureau IA PixelKréol

Il pose **200 questions fictives** (8 par agent, 25 agents) à l'orchestrateur du serveur, note chaque réponse et affiche le score par agent.
Il sert à vérifier une modification de fiche (`agents/registre.yaml`) **avant / après**, et à repérer ce qui dérape (prix faux, envoi sans validation, secret recopié…).

## Où et comment le lancer *(terminal Hostinger, en administrateur)*
```bash
cd /opt/pixelkreol-bureau-ia && python3 scripts/banc_agents.py
```
Rien à installer (Python 3 seulement). Il lit la clé dans le `.env` du dossier. Compter **20 à 40 minutes** (les modèles gratuits répondent lentement et se saturent) ;
`Ctrl+C` arrête. Chaque réponse est ensuite notée.

| Pour… | Commande (même dossier) |
|---|---|
| Un essai rapide (3 questions par agent, ≈ 10 min) | `python3 scripts/banc_agents.py --rapide` |
| Un seul agent ou deux | `python3 scripts/banc_agents.py --agent devis,commercial` |
| Voir les vérifications ratées cas par cas | `python3 scripts/banc_agents.py --detail` |
| **Mesurer avant / après un changement de fiche** | 1) `python3 scripts/banc_agents.py --sauver /root/banc-avant.json` 2) copier les nouvelles fiches (voir `DEPLOIEMENT.md` : le dossier du serveur n'est pas une copie git ; l'orchestrateur relit `agents/` à chaud, sans redémarrage) 3) `python3 scripts/banc_agents.py --comparer /root/banc-avant.json --sauver /root/banc-apres.json` |
| Ajouter l'avis d'un modèle (« juge ») | `python3 scripts/banc_agents.py --juge` |
| Vérifier aussi que `pk-auto` choisit le bon agent | `python3 scripts/banc_agents.py --routage` |
| Lister les questions sans rien appeler | `python3 scripts/banc_agents.py --liste` |

## Ce qui est automatique, ce qui ne l'est pas
- **Règles (automatique, déterministe)** : à chaque question, quelques vérifications écrites à l'avance — les **prix exacts** de la grille sont présents et **aucun montant inventé** n'apparaît, la ligne
  « Feu : ORANGE/ROUGE » est là quand il faut, le bon agent est cité en renvoi, la demande dangereuse est refusée, le format demandé (tableau, étapes, objet de mail, YAML) est respecté,
  aucune coordonnée ni statistique inventée, vouvoiement des textes pour un client. Le score d'un agent est la part de vérifications réussies sur ses 8 questions.
  Les règles lisent le texte : une bonne réponse formulée très différemment peut perdre un point, une mauvaise réponse habile peut en gagner un. **C'est un instrument de comparaison avant/après, pas un jugement.**
- **Juge IA (`--juge`, NON déterministe)** : un modèle (pk-costaud) note 0 à 5 selon les critères écrits pour chaque question. Son avis varie d'un passage à l'autre ; il est affiché dans une colonne
  séparée et ne change jamais le score des règles. La réponse de l'agent lui est donnée comme une donnée à évaluer, jamais comme une consigne.
- **Routage (`--routage`)** : appelle `/route` de l'orchestrateur pour chaque question qui doit atteindre un agent précis.
- **Non automatique** : le fond (une réponse juste mais mal tournée), le ton, la pertinence métier. Lire les réponses enregistrées (`--sauver`, fichier JSON) pour les cas qui comptent.

## Variance et coût
Les modèles gratuits ne répondent pas deux fois la même chose : sur 8 questions par agent, **un écart de ±10 points est du bruit**. Ne conclure qu'à partir de l'ensemble (ligne GLOBAL) ou d'écarts répétés.
Chaque passage fait ~200 appels (plus ~200 avec `--juge`) sur les fournisseurs gratuits : le banc attend 1 s entre deux questions (`--pause`) et retente après 5, 15 puis 30 s en cas de saturation (429).
Une question sans réponse est signalée à part et **n'est pas comptée comme un zéro**.

## Mesures hors ligne (sans aucun modèle), pour le développement
`python tests/mesure_agents.py [--agents DOSSIER] [--detail]` (dépôt de développement) : routage de `pk-auto` sur les questions du banc, avec les mots-clés seuls (cas le plus défavorable),
et audit de complétude des fiches (10 critères : mission, format, feux, renvois, interdits, cas limites, exemple, cadrage, honnêteté, ton).
Ce n'est **pas** la qualité des réponses : c'est une grille de lecture des fiches, utile pour comparer deux versions sans serveur.

## Ajouter ou corriger une question
Les cas sont dans `scripts/banc/cas_*.py` : `C(agent, identifiant, question, critères, vérification, …, route=…)`. Les vérifications disponibles sont dans `scripts/banc/regles.py`
(`prix`, `montants_ok`, `feu`, `renvoie`, `refuse`, `structure`, `contient`, `un_de`, `absent`, `mots_max`, `vouvoie`, `pose_question`…). `python -m pytest tests/test_banc_agents.py` vérifie que chaque règle est valide.
Les prix de la grille (`GRILLE` dans `regles.py`) sont ceux du registre : les changer ici ne change aucune fiche.
