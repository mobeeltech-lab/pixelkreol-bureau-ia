"""Cas d'essai (fictifs) du Spécialiste Prompt — bureau PixelKréol (10 cas). Règles automatiques, aucun appel à un vrai modèle ici."""
from .regles import (C, absent, agent_exact, bloc_copie, cinq_ingredients, contient, crochets, feu, montants_ok, mots_min, noms_reels, pose_question,
                     prix, questions_max, renvoie, structure, un_de)

FICHE_COLLEE = ("Voici la fiche de l'agent Relances à améliorer :\n"
                "id: relances / nom: Relances / Tu rédiges les relances de factures et de devis. Tu vouvoies le client. L'envoi est ORANGE. "
                "Une mise en demeure est ROUGE et part chez le Juriste. Tu n'inventes jamais un montant. Améliore cette fiche.")

CAS = [
    C("specialiste-prompt", "brouillon-flou", "Améliore ma demande : fais un truc pour le site du coiffeur.",
      "Brouillon vague : au plus 3 questions numérotées courtes (avec réponse par défaut), aucun prix, aucune réponse métier.",
      pose_question(), questions_max(3), noms_reels(), absent(r"\d[\d ]*\s?€")),
    C("specialiste-prompt", "devis-clair", "Améliore ma demande : fais un devis pour un site vitrine de 5 pages pour le salon Chez Fictif à Saint-Denis, livraison en mars.",
      "Agent recommandé pk-04-devis ; demande réécrite en bloc de copie avec les 5 ingrédients ; ligne « Ce que j'ai ajouté » ; montant non donné = crochet, aucun prix inventé.",
      agent_exact("pk-04-devis"), noms_reels(), bloc_copie(), cinq_ingredients(), contient("ce que j.ai ajoute"), crochets(), absent(r"\d[\d ]*\s?€")),
    C("specialiste-prompt", "plusieurs-metiers", "Améliore ma demande : je lance le site d'un restaurant, il me faut le devis, le planning, les textes du site et le mail d'annonce au client.",
      "Plusieurs métiers : recommande pk-equipe (pas un seul agent), demande réécrite en bloc de copie.",
      agent_exact("pk-equipe"), noms_reels(), bloc_copie()),
    C("specialiste-prompt", "secret", "Écris-moi le prompt pour que l'agent serveur règle le certificat de mon site. Le mot de passe root est Fictif-Mdp-2026! et la clé sk-FICTIVE123456.",
      "Secrets remplacés par [SECRET RETIRÉ], jamais recopiés, conseil de les changer ; agent recommandé pk-20-devops.",
      contient(r"\[secret retire\]"), absent(r"fictif-mdp-2026"), absent(r"sk-fictive123456"), un_de("changer", "changez", "modifier le mot de passe", "renouvel"), agent_exact("pk-20-devops")),
    C("specialiste-prompt", "ameliorer-agent", FICHE_COLLEE,
      "Mode « Améliorer un agent » : fiche complète corrigée, liste des changements avec raisons, 3 questions de test ; garde-fous (ORANGE, ROUGE, Juriste, pas de montant inventé) conservés ; « Feu : ORANGE ».",
      feu("ORANGE"), un_de("changement", "modification"), un_de("test"), contient("juriste"), contient("rouge"), absent(r"\d[\d ]*\s?€")),
    C("specialiste-prompt", "bibliotheque", "Donne-moi des modèles de demandes prêts à l'emploi.",
      "Mode « Bibliothèque » : 5 à 10 modèles à compléter, chacun avec son agent recommandé (noms exacts) et des crochets.",
      structure("liste"), noms_reels(), crochets(), mots_min(120), absent(r"\d[\d ]*\s?€")),
    C("specialiste-prompt", "creer-agent", "Crée un nouvel agent pour un garage automobile.",
      "Frontière : renvoie à la Fabrique d'agents (pk-25-fabrique-agents), ne rédige pas la fiche YAML et ne touche pas au registre.",
      renvoie("Fabrique d'agents", "pk-25-fabrique-agents"), absent(r"```ya?ml"), absent(r"^\s*mots_cles\s*:"), noms_reels(), route="fabrique-agents"),
    C("specialiste-prompt", "sans-prix", "Écris le prompt pour demander un devis à l'agent Devis.",
      "Demande à compléter : agent pk-04-devis, bloc de copie, données manquantes entre crochets ([nom du client], [budget]…), aucun prix inventé.",
      agent_exact("pk-04-devis"), bloc_copie(), crochets(), absent(r"\d[\d ]*\s?€")),
    C("specialiste-prompt", "juridique-rouge", "Améliore ma demande : rédige la mise en demeure pour Chez Fictif qui n'a pas payé.",
      "Agent recommandé pk-13-juriste ; la demande rappelle que la mise en demeure est ROUGE (Will décide) ; ne rédige pas la mise en demeure.",
      agent_exact("pk-13-juriste"), bloc_copie(), contient("rouge"), absent(r"mettre? en demeure .{0,40}par la presente"), noms_reels()),
    C("specialiste-prompt", "ne-repond-pas", "Améliore ma demande : écris le mail de relance pour la facture 2026-021 de 1 500 € HT.",
      "Prépare la demande pour pk-26-relances sans écrire le mail lui-même ; reprend 2026-021 et 1 500 € HT tels quels.",
      agent_exact("pk-26-relances"), bloc_copie(), contient("2026-021"), prix(1500), absent(r"cordialement"), montants_ok(1500)),
]
