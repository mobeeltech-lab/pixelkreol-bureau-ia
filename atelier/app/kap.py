"""Mention Kap Numérik sur les devis : suspendu depuis décembre 2025, sans montant, sauf réglage KAP_NUMERIK_OUVERT=true."""
import html

from . import config

SUSPENDU = "Kap Numérik suspendu depuis décembre 2025, à vérifier auprès de la Région."


def bloc(texte_kap: str | None) -> str:
    """Bloc HTML de la mention Kap Numérik d'un devis ('' si le devis n'en porte pas)."""
    if not texte_kap:
        return ""
    if config.KAP_OUVERT:
        return (f'<div class="box small" style="margin-top:8px"><b>Financement Kap Numérik (estimation) :</b> {html.escape(texte_kap)} '
                "— sous réserve d'acceptation du dossier par la Région Réunion.</div>")
    return f'<div class="box small" style="margin-top:8px"><b>{SUSPENDU}</b> Aucun financement n\'est promis.</div>'
