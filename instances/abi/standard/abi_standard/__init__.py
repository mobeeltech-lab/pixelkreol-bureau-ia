"""Standard téléphonique IA d'ABI Holding — phase 1 : messages vocaux (asynchrone).

Relève une boîte mail dédiée (messagerie vocale de l'opérateur → e-mail avec l'audio en pièce jointe), transcrit
EN LOCAL, confie la transcription à la Secrétaire, prévient la bonne personne et crée une tâche de rappel.
Il ne rappelle jamais un client et n'écrit jamais à un client : il PRÉPARE.
"""
VERSION = "1.0.0"
