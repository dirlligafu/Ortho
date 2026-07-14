# Suivi des branches et PR (aide-mémoire personnel)

Dernière mise à jour : 2026-07-14. Commité sur les branches de travail en cours pour suivi entre machines, mais ne doit jamais faire partie d'une PR proposée au dépôt de 6wheel : à retirer avant d'ouvrir chaque PR concernée.

## État des lieux

| Branche | Poussée sur le fork | PR ouverte | Contenu |
|---|---|---|---|
| `fix-part-count-on-individual-toggle` | oui | #5 | Compteur "included" qui ne se mettait pas à jour |
| `section-markers` | oui | #2 | Marqueurs numérotés S1/S2... sur les vues, avec préférence globale |
| `three-js-preview` | oui | #3 | Aperçu 3D interactif (Three.js) dans la section Parts |
| `fix-hidden-line-depth-eps-scale` | oui | #7 | Tolérance de lignes cachées relative à l'échelle du modèle + bascule wireframe |
| `fix-rib-marker-position` | oui | #11 | Correction du décalage de padding sur les traits de coupe (1er/dernier trait) |
| `fix-obj-ngon-triangulation` | oui | #12 | Triangulation des faces quad/n-gon à l'import OBJ |
| `template-presets` | oui | **non retenue volontairement** | Système de gabarits JSON, cartouche façon plan technique |
| `split-view-export` | oui (juste le découpage d'images) | **non, en attente de #2** | Export ZIP images séparées par vue + coupes |
| `blender-reference-script` | **non, local uniquement** | — | Script Blender auto-généré (positionnement 3D, collection dédiée), basé sur `split-view-export` |
| `longitudinal-section` | **non, local uniquement** | — | Nouvelle coupe centrale (axe gauche/droite) dans l'image composite, indépendante des deux ci-dessus |

## Qui touche quoi (`compositor.py` est la zone chaude)

| Fichier | Branches qui le touchent |
|---|---|
| `compositor.py` | section-markers, fix-rib-marker-position, template-presets, split-view-export, blender-reference-script, longitudinal-section |
| `app.py` | section-markers, fix-hidden-line-depth-eps-scale, template-presets, split-view-export, blender-reference-script, longitudinal-section |
| `templates/index.html` | quasiment toutes les branches |
| `renderer.py` | fix-hidden-line-depth-eps-scale, longitudinal-section |
| `model_loader.py` | fix-obj-ngon-triangulation (seule) |

`templates/index.html` étant touché par presque tout le monde, les conflits y sont fréquents mais généralement bénins (chaque branche ajoute sa propre case/ligne à un endroit différent). Les vrais points de friction sont dans `compositor.py`, détaillés ci-dessous.

## Les deux vraies zones de conflit

### 1. La ligne `fracs_for_view` dans `compose_image()` (calcul des traits de coupe sur les vues)

Quatre branches modifient indépendamment cette même ligne, chacune avec une combinaison différente :

- `section-markers` (#2) : ajoute les tuples `(frac, num)` + labels S1/S2, **sans** la correction de padding
- `fix-rib-marker-position` (#11) : ajoute la correction de padding, **sans** les tuples/labels
- `split-view-export` : a les **deux** (tuples + correction de padding) — anticipe déjà l'état combiné
- `longitudinal-section` : a la correction de padding portée depuis #11 (décision volontaire, voir conversation), sans les tuples

Concrètement : #2 et #11 vont se percuter l'une l'autre en amont dès que l'une des deux sera mergée, peu importe l'ordre. Ce n'est pas un vrai désaccord de fond, juste deux ajouts différents à la même ligne, donc résolution triviale mais nécessaire.

### 2. La signature de `compose_image()`

- `template-presets` ajoute un paramètre pour le gabarit/cartouche
- `longitudinal-section` ajoute `axis_cfg` (obligatoire) et `longitudinal_segments`

Ces deux branches vont aussi se percuter sur la ligne de signature elle-même.

## Ordre de merge recommandé

1. **D'abord, sans risque, dans n'importe quel ordre** : `fix-part-count-on-individual-toggle` (#5), `fix-obj-ngon-triangulation` (#12), `fix-hidden-line-depth-eps-scale` (#7), `three-js-preview` (#3). Aucune ne touche la zone chaude de `compositor.py`.

2. **Ensuite, une des deux `#2`/`#11`** (mon conseil : `section-markers` #2 en premier, puisque `split-view-export` en dépend déjà explicitement). Dès qu'elle est mergée en amont :
   - Rebase immédiatement l'autre (#11 si #2 est passée en premier) sur le nouveau `main`, en réconciliant à la main la ligne `fracs_for_view` (ajouter la correction de padding par-dessus les tuples déjà là, ou l'inverse). C'est toi qui fais ce travail, jamais le mainteneur.

3. Une fois **#2 et #11 toutes les deux mergées** en amont :
   - Rebase `split-view-export` sur le nouveau `main` : sa duplication (tuples + padding) devrait alors se résorber quasi automatiquement puisque `main` contient déjà les deux.
   - Rebase `longitudinal-section` sur le nouveau `main` : retirer sa propre copie de la correction de padding (devenue redondante), ne garder que ses ajouts propres (nouvelle coupe, signature `compose_image`).
   - Ouvre la PR de `split-view-export` à ce moment-là.

4. **`longitudinal-section`** peut être proposée en PR dès l'étape 3 terminée, indépendamment de `split-view-export`/`blender-reference-script`.

5. **`blender-reference-script`** reste derrière `split-view-export` (elle en dépend directement) — à rebaser/proposer une fois que `split-view-export` est mergée ou au moins stable.

6. **`template-presets`** : au-delà du chevauchement avec `longitudinal-section` sur la signature de `compose_image()` (facile à résoudre, deux nouveaux paramètres indépendants), elle reste retenue pour une autre raison (pas sûr que l'approche te convienne / convienne au mainteneur) — à traiter séparément, pas de contrainte d'ordre technique forte avec les autres.

## Points à surveiller

- Ne jamais laisser le mainteneur tomber sur un de ces conflits via le bouton merge de GitHub — toujours rebaser proactivement soi-même en amont.
- Avant d'ouvrir une nouvelle PR, revérifier ce fichier : si une branche dont elle dépend a été mergée entre-temps, la rebase d'abord.
- Ce fichier est à mettre à jour à la main (ou à me redemander de le faire) à chaque fois qu'une PR est mergée en amont ou qu'une nouvelle branche est créée.
