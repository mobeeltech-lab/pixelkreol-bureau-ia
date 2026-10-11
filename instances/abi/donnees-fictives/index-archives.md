DONNÉES FICTIVES — TEST

# Index des archives (FICTIF) — simule le contenu d'un NAS

## 1. Liste de fichiers fournie à l'Archiviste

```
/ABI/Oliviers/acquereurs/FICTIF-DURAND/contrat-reservation.pdf
/ABI/Oliviers/acquereurs/FICTIF-DURAND/Acte VEFA signé.pdf
/ABI/Oliviers/acquereurs/FICTIF-DURAND/acte vefa signe (copie).pdf
/ABI/Oliviers/acquereurs/EXEMPLE-MARTIN/2025-12-09_oliviers_A05_acte-vefa_v1.pdf
/ABI/Oliviers/acquereurs/EXEMPLE-MARTIN/2025-12-09_oliviers_A05_acte-vefa_v2.pdf
/ABI/Oliviers/acquereurs/TEST-ROSSI/scan0012.pdf
/ABI/Oliviers/acquereurs/FACTICE-SANTONI/contrat-reservation.pdf
/ABI/Oliviers/acquereurs/IMAGINAIRE-POLI/acte-vefa-B08.pdf
/ABI/Oliviers/chantier/attestation avancement fondations.pdf
/ABI/Oliviers/chantier/2026-09-24_oliviers_hors-d-eau_attestation_v1.pdf
/ABI/Oliviers/chantier/situation-travaux-4-maconnerie.xlsx
/ABI/Oliviers/chantier/situation-travaux-4-maconnerie - Copie.xlsx
/ABI/Oliviers/chantier/planning-v3.xlsx
/ABI/Oliviers/chantier/planning-v3-FINAL.xlsx
/ABI/Oliviers/chantier/planning-v3-FINAL-vraiment.xlsx
/ABI/Oliviers/reserves/reserves-batiment-A-2026-10-05.pdf
/ABI/Cypres/livraison/PV-livraison-CYP-B12.pdf
/ABI/Cypres/livraison/pv livraison cyp-b07.pdf
/ABI/Cypres/baux/BAIL-A_CYP-A03_signe.pdf
/ABI/Cypres/baux/BAIL-B_CYP-B07_signe.pdf
/ABI/Cypres/baux/bail C.docx
/ABI/Comptabilite/2026-09_releve-bancaire.pdf
/ABI/Comptabilite/FICT-2026-117_menuiseries.pdf
/ABI/Divers/scan0013.pdf
/ABI/Divers/permis-de-construire-pieces.zip
```

## 2. Pièces attendues par dossier d'acquéreur (liste type FICTIVE, à valider par la direction)

1. Contrat de réservation
2. Acte de VEFA signé
3. Pièce d'identité
4. Justificatif de financement (accord de prêt ou attestation)
5. Appels de fonds émis et justificatifs de paiement

## 3. Ce que l'Archiviste doit détecter

- Doublons : `Acte VEFA signé.pdf` / `acte vefa signe (copie).pdf` ; trois versions de `planning-v3` ;
  `situation-travaux-4` et sa copie.
- Noms non conformes à la règle `AAAA-MM-JJ_programme_lot_type_vN` : la plupart des fichiers.
- Fichiers sans nom parlant : `scan0012.pdf` (dossier TEST-ROSSI), `scan0013.pdf` (Divers).
- Pièces manquantes : le dossier FACTICE-SANTONI ne contient pas d'acte de VEFA ; aucun dossier ne contient de
  justificatif de financement ni de pièce d'identité dans cette liste.
- Document introuvable : il n'existe pas de PV de livraison pour un lot des Oliviers (non livrés).
