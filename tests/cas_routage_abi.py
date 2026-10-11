"""Demandes réalistes (promotion immobilière, Ajaccio) et agent attendu pour pk-auto — instance ABI Holding.

Tout est FICTIF. « Attendu » = le jugement de l'auditeur à partir des missions des 9 agents.
CAS_CONTROLE a été écrit AVANT les mots-clés et ne sert pas à les régler.
"""
CAS = [
    # --- direction ------------------------------------------------------------
    ("Fais-moi le point du jour et les priorités de la semaine", "orchestrateur"),
    ("Prépare la synthèse quotidienne pour la direction", "orchestrateur"),
    ("Qui doit faire quoi sur le programme des Oliviers cette semaine ?", "orchestrateur"),
    ("Répartis le travail entre les agents pour la clôture du trimestre", "orchestrateur"),
    ("Bonjour !", "orchestrateur"),
    # --- secrétaire -----------------------------------------------------------
    ("Rédige un courrier type pour informer les acquéreurs de la réunion de chantier", "secretaire"),
    ("Réponds à ce mail du fournisseur de menuiseries", "secretaire"),
    ("Rappelle-moi les échéances de la semaine prochaine", "secretaire"),
    ("Trie les mails reçus ce matin et dis-moi lesquels sont urgents", "secretaire"),
    ("Prépare le compte rendu de la réunion de lundi", "secretaire"),
    ("Mets le rendez-vous avec le notaire dans l'agenda", "secretaire"),
    # --- comptable ------------------------------------------------------------
    ("Prépare les écritures comptables de la facture du terrassier", "comptable"),
    ("Fais le rapprochement bancaire du mois de septembre", "comptable"),
    ("Prépare l'export pour l'expert-comptable", "comptable"),
    ("Quel compte utiliser pour les retenues de garantie des entreprises ?", "comptable"),
    ("Où en est la trésorerie de la SCCV ?", "comptable"),
    ("Enregistre la facture de l'électricien en comptabilité", "comptable"),
    # --- appels de fonds ------------------------------------------------------
    ("Prépare l'appel de fonds hors d'eau pour les Oliviers", "appels-de-fonds"),
    ("Calcule l'échéancier VEFA du lot A05", "appels-de-fonds"),
    ("Rédige la lettre d'appel de fonds pour M. Fictif-Durand", "appels-de-fonds"),
    ("Quels acquéreurs n'ont pas encore payé l'appel de fondations ?", "appels-de-fonds"),
    ("Vérifie que l'échéancier respecte les plafonds de l'article R261-14", "appels-de-fonds"),
    ("Fais le suivi des encaissements des appels de fonds", "appels-de-fonds"),
    ("Relance l'acquéreur du lot B03 pour son appel de fonds en retard", "appels-de-fonds"),
    # --- commercial -----------------------------------------------------------
    ("Rédige une annonce pour le T3 du bâtiment B", "commercial"),
    ("Réponds à ce prospect qui demande le prix d'un T2", "commercial"),
    ("Mets à jour le fichier des réservations", "commercial"),
    ("Quels lots sont encore disponibles à la vente ?", "commercial"),
    ("Prépare un message de présentation du programme pour un investisseur", "commercial"),
    # --- gestionnaire locatif --------------------------------------------------
    ("Révise le loyer de M. Fictif selon l'IRL", "locatif"),
    ("Prépare la quittance de loyer de septembre pour Mme Test-Locataire", "locatif"),
    ("Le locataire du lot B07 a un mois de retard de loyer, relance-le", "locatif"),
    ("Régularise les charges locatives de l'année", "locatif"),
    ("Prépare le renouvellement du bail du lot A03", "locatif"),
    # --- juriste --------------------------------------------------------------
    ("Relis ce contrat de réservation avant signature", "juriste"),
    ("Prépare le dossier pour l'avocat sur le litige avec l'entreprise de gros œuvre", "juriste"),
    ("Fais une veille sur les nouveautés de la loi sur la VEFA", "juriste"),
    ("Quelles clauses faut-il vérifier dans ce marché de travaux ?", "juriste"),
    # bail + relecture : le Juriste l'emporte sur le Gestionnaire locatif
    ("Relis le bail du lot CYP-A03", "juriste"),
    ("Peux-tu relire ce bail avant que je le fasse signer ?", "juriste"),
    ("Relecture du bail de Mme Test-Locataire", "juriste"),
    ("Fais la relecture du bail commercial", "juriste"),
    ("Vérifie les clauses du bail", "juriste"),
    ("Vérifie les clauses du contrat de VEFA", "juriste"),
    ("Relis le contrat de bail avant signature", "juriste"),
    ("Relisez les baux des trois logements loués", "juriste"),
    ("Relis le bail et dis-moi si la clause de révision est correcte", "juriste"),
    # --- suivi de programme ----------------------------------------------------
    ("Mets à jour le planning du chantier", "suivi-programme"),
    ("Prépare le tableau de bord hebdomadaire du programme", "suivi-programme"),
    ("Liste les réserves de la visite de pré-livraison du bâtiment A", "suivi-programme"),
    ("Contrôle la situation de travaux numéro 4 de l'entreprise de maçonnerie", "suivi-programme"),
    ("La mise hors d'eau est-elle atteinte ? Prépare ce qu'il faut pour les appels de fonds", "suivi-programme"),
    # --- archiviste -----------------------------------------------------------
    ("Retrouve le PV de livraison du lot B12", "archiviste"),
    ("Classe les documents scannés dans le dossier du programme", "archiviste"),
    ("Y a-t-il des doublons dans le dossier des acquéreurs ?", "archiviste"),
    ("Quelles pièces manquent dans le dossier de M. Fictif ?", "archiviste"),
    ("Renomme ces fichiers selon notre règle de nommage", "archiviste"),

    # --- Spécialiste Prompt : 10 demandes qui doivent y aller, puis 10 voisines qui ne doivent PAS y aller
    ("Améliore ma demande : relance les gens qui n'ont pas payé", "specialiste-prompt"),
    ('Écris-moi le prompt pour préparer un appel de fonds', "specialiste-prompt"),
    ("Reformule ma demande pour l'agent Juriste", "specialiste-prompt"),
    ('Quelle est la meilleure façon de demander un courrier aux acquéreurs ?', "specialiste-prompt"),
    ("Ma demande est floue, aide-moi à la préciser avant de l'envoyer", "specialiste-prompt"),
    ("Donne-moi des modèles de demandes prêts à l'emploi", "specialiste-prompt"),
    ('Je voudrais une bibliothèque de prompts pour la direction', "specialiste-prompt"),
    ("Améliore la fiche de l'agent Archiviste, voici son texte", "specialiste-prompt"),
    ('Comment demander à un agent de faire le point sur le chantier ?', "specialiste-prompt"),
    ('Améliore mon prompt : fais le courrier', "specialiste-prompt"),
    ("Prépare l'appel de fonds de la mise hors d'eau", 'appels-de-fonds'),
    ('Relis le bail du locataire du T2', 'juriste'),
    ('Rédige un courrier type pour les acquéreurs', 'secretaire'),
    ('Retrouve le PV de livraison du lot C07', 'archiviste'),
    ('Fais-moi le point du jour', 'orchestrateur'),
    ('Rédige une annonce pour un appartement de 3 pièces', 'commercial'),
    ('Fais la quittance du locataire du lot A03', 'locatif'),
    ('Mets à jour le planning du chantier de la tranche 2', 'suivi-programme'),
    ('Passe les écritures comptables de la facture du terrassier', 'comptable'),
    ('Relis le contrat de réservation du lot B12', 'juriste'),
]

CAS_CONTROLE = [
    ("Prépare l'apel de fond de la mise hors d'eau", "appels-de-fonds"),
    ("Envoie un rappel aux acquéreurs pour les paiements du stade achèvement", "appels-de-fonds"),
    ("Fais la quitance du locataire du T2", "locatif"),
    ("Le loyé de ce mois n'est pas payé par le locataire du lot A03", "locatif"),
    ("Retrouve l'attestation d'avancement des fondations", "archiviste"),
    ("Passe les écritures de la facture d'acompte du maçon", "comptable"),
    ("Rédige un mail de réponse à la banque", "secretaire"),
    ("Annonce pour un appartement de 3 pièces avec terrasse", "commercial"),
    ("Relis le bail avant que le locataire signe", "juriste"),
    ("Où en est l'avancement du chantier du bâtiment B ?", "suivi-programme"),
    ("Donne-moi un résumé de ce qui est urgent aujourd'hui", "orchestrateur"),
    ("Y a-t-il un doublon pour la fiche du lot B12 dans les archives ?", "archiviste"),
]
