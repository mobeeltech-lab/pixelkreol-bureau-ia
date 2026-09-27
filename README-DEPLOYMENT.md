# PixelKréol — Bureau IA Local — Guide de Déploiement

## 📋 Fichiers prêts pour la mise en ligne

Tous les fichiers HTML sont optimisés et prêts pour une mise en ligne immédiate :

### Pages principales
- **index-improved.html** → Accueil (à renommer `index.html`)
- **pixelkreol-bureau-ia-improved.html** → Page produit Bureau IA
- **pixelkreol-agents-communication.html** → Page d'interaction avec les agents IA
- **secteurs-improved.html** → Packs par secteur d'activité (38 packs × 11 secteurs)
- **tarification-bureau-ia.html** → Grille tarifaire complète + exemples

## 🚀 Déploiement sur Vercel (recommandé)

### Étape 1 : Préparer les fichiers
```bash
# Renommer la page d'accueil
mv index-improved.html index.html
mv pixelkreol-bureau-ia-improved.html bureau-ia.html
mv secteurs-improved.html secteurs.html
```

### Étape 2 : Créer un projet Vercel
1. Aller à https://vercel.com
2. Cliquer sur "New Project"
3. Importer depuis GitHub ou uploader les fichiers
4. Configurer le domaine (optionnel mais recommandé)

### Étape 3 : Déployer
```bash
# Avec Vercel CLI
npm i -g vercel
vercel deploy
```

## 🔗 Liens internes (à mettre à jour après déploiement)

Une fois en ligne, remplacer les chemins locaux :

```html
<!-- Avant (local) -->
<a href="pixelkreol-bureau-ia.html">

<!-- Après (en ligne sur Vercel) -->
<a href="https://votre-domaine.vercel.app/bureau-ia.html">
```

## 📊 Tarification — Version actuelle (Septembre 2026)

| Pack | Prix | Agents | Matériel |
|------|------|--------|----------|
| **Assistant Maison** | 1 990 € TTC | 3 | PC reconditionné + accélérateur |
| **Bureau Essentiel** | 5 500 € HT | 5 | GEEKOM A9 Max+388 + RTX 5060 Ti |
| **Bureau Complet** | 13 900 € HT | 9 + 1 service | GEEKOM A9 Max+388 + RTX 5060 Ti |
| **Sur-mesure** | À partir de 18 000 € HT | 10+ | Selon besoins |

## 🎨 Charte graphique

**Couleurs principales**
- Lagon : `#0B5D6B`
- Lagon vif : `#16A6B6`
- Volcan (noir) : `#1B1A17`
- Corail : `#E0562B`

**Typographie**
- Headings : Outfit (500, 700)
- Body : Source Sans 3 (400, 600)

## 📱 Responsive Design

Tous les fichiers sont 100% responsifs :
- ✓ Mobile (< 480px)
- ✓ Tablette (480px - 768px)
- ✓ Desktop (> 768px)

## 💾 Points importants

1. **Images** : Ajouter des images au PDF tarifaire (tarification-bureau-ia.html) avec `<img>` ou CSS `background-image`
2. **SEO** : Métadonnées JSON-LD intégrées sur toutes les pages
3. **Accessibilité** : WCAG 2.1 AA compliant
4. **Performance** : Fichiers légers (< 100 KB chacun)

## 📞 Contact

Email : mobeeltech@gmail.com
Téléphone : À ajouter
Localisation : La Réunion (974), France

## 🔄 Mise à jour des tarifs

Pour mettre à jour les tarifs, modifier :
1. `index-improved.html` (ligne ~333)
2. `pixelkreol-bureau-ia-improved.html` (lignes ~414, 430, 446)
3. `tarification-bureau-ia.html` (tableau + détails packs)

---

**Version** : 1.0 - Septembre 2026
**État** : Prêt pour production ✅
