"""Configuration de PK Atelier (lue dans les variables d'environnement / fichier .env)."""
import os
from pathlib import Path


def _b(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "oui", "yes", "on")


DATA = Path(os.getenv("ATELIER_DATA", "/data"))
API_KEY = os.getenv("ATELIER_KEY", "")
SECRET = os.getenv("ATELIER_SECRET", API_KEY or "change-moi")
URL_PUBLIQUE = os.getenv("ATELIER_URL_PUBLIQUE", "https://atelier.mebeeltech.online").rstrip("/")
URL_DEMO = os.getenv("ATELIER_URL_DEMO", "https://demo.mebeeltech.online").rstrip("/")
HOTE_DEMO = os.getenv("ATELIER_HOTE_DEMO", "demo.mebeeltech.online")
FUSEAU = os.getenv("ATELIER_FUSEAU", "Indian/Reunion")

# Vendeur (mentions légales des factures)
V = {
    "nom": os.getenv("VENDEUR_NOM", "PixelKréol"),
    "forme": os.getenv("VENDEUR_FORME", ""),  # ex. "EI", "SASU au capital de 1 000 €"
    "adresse": os.getenv("VENDEUR_ADRESSE", "La Réunion"),
    "cp_ville": os.getenv("VENDEUR_CP_VILLE", ""),
    "pays": os.getenv("VENDEUR_PAYS", "FR"),
    "siren": os.getenv("VENDEUR_SIREN", ""),
    "siret": os.getenv("VENDEUR_SIRET", ""),
    "rcs": os.getenv("VENDEUR_RCS", ""),
    "tva": os.getenv("VENDEUR_TVA", ""),
    "email": os.getenv("VENDEUR_EMAIL", "mobeeltech@gmail.com"),
    "tel": os.getenv("VENDEUR_TEL", ""),
    "site": os.getenv("VENDEUR_SITE", "https://pixelkreol.mebeeltech.online"),
    "iban": os.getenv("VENDEUR_IBAN", ""),
    "bic": os.getenv("VENDEUR_BIC", ""),
}
FRANCHISE_TVA = _b("VENDEUR_FRANCHISE_TVA", "false")
TVA_DEFAUT = float(os.getenv("TVA_DEFAUT", "8.5"))
PENALITES = os.getenv(
    "MENTION_PENALITES",
    "En cas de retard de paiement, des pénalités au taux de trois fois le taux d'intérêt légal sont exigibles, "
    "ainsi qu'une indemnité forfaitaire pour frais de recouvrement de 40 € (clients professionnels). "
    "Pas d'escompte pour paiement anticipé.",
)

# Mails
SMTP = {
    "hote": os.getenv("SMTP_HOTE", ""),
    "port": int(os.getenv("SMTP_PORT", "587")),
    "utilisateur": os.getenv("SMTP_UTILISATEUR", ""),
    "mot_de_passe": os.getenv("SMTP_MOT_DE_PASSE", ""),
    "ssl": _b("SMTP_SSL", "false"),
    "expediteur": os.getenv("SMTP_EXPEDITEUR", os.getenv("SMTP_UTILISATEUR", "")),
    "nom_expediteur": os.getenv("SMTP_NOM_EXPEDITEUR", "PixelKréol"),
}
MAIL_VALIDATION = _b("MAIL_VALIDATION", "true")  # true = chaque mail attend le OUI de Will
MAIL_NOTIFIER = os.getenv("MAIL_NOTIFIER", "")  # adresse qui reçoit les demandes de validation
MAIL_LISTE_BLANCHE = [x.strip().lower() for x in os.getenv("MAIL_LISTE_BLANCHE", "").split(",") if x.strip()]
MAIL_MAX_PAR_JOUR = int(os.getenv("MAIL_MAX_PAR_JOUR", "50"))

# Sites démo
SITE_JOURS_DEFAUT = int(os.getenv("SITE_JOURS_DEFAUT", "30"))
SITE_TAILLE_MAX = int(os.getenv("SITE_TAILLE_MAX_MO", "15")) * 1024 * 1024

# Hébergement autonome (service hôte pk-hebergeur : nginx + certificats HTTPS automatiques)
HEBERGEMENT_ACTIF = _b("HEBERGEMENT_ACTIF", "false")          # posé par l'installeur quand pk-hebergeur tourne
SOUS_DOMAINES = _b("HEBERGEMENT_SOUS_DOMAINES", "false")      # true quand *.demo.<domaine> pointe vers le VPS
IP_VPS = os.getenv("ATELIER_IP_VPS", "")
HEBERG = DATA / "hebergement"

for d in ("fichiers", "sites", "hebergement/demandes", "hebergement/attente", "hebergement/etat"):
    (DATA / d).mkdir(parents=True, exist_ok=True)
