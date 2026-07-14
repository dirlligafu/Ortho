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
| `fix-compositor-axis-hardcoding` | oui | #13 | Correction des indices d'axes en dur dans les panneaux de coupe pour `up_axis` non standard |
| `template-presets` | oui | **non retenue volontairement** | Système de gabarits JSON, cartouche façon plan technique |
| `split-view-export` | oui (juste le découpage d'images) | **non, en attente de #2** | Export ZIP images séparées par vue + coupes |
| `blender-reference-script` | **non, local uniquement** | — | Script Blender auto-généré (positionnement 3D, collection dédiée), basé sur `split-view-export` |
| `longitudinal-section` | **non, local uniquement** | — | Nouvelle coupe centrale (axe gauche/droite) dans l'image composite, indépendante des deux ci-dessus |

## Qui touche quoi (`compositor.py` est la zone chaude)

| Fichier | Branches qui le touchent |
|---|---|
| `compositor.py` | section-markers, fix-rib-marker-position, fix-compositor-axis-hardcoding, template-presets, split-view-export, blender-reference-script, longitudinal-section |
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
- `fix-compositor-axis-hardcoding` (#13) ajoute aussi `axis_cfg` (obligatoire) — c'est la même chose que `longitudinal-section`, extraite dans sa propre PR (voir conversation du 2026-07-14)

Ces branches vont aussi se percuter sur la ligne de signature elle-même.

### 3. `axis_cfg` devient obligatoire dans `compose_image()` (nouveau, depuis #13)

Une fois #13 mergée en amont, **tout appelant** de `compose_image()` doit lui passer `axis_cfg`. **`longitudinal-section` est déjà rebasée sur #13 (fait le 2026-07-14, testé, poussée avec force-with-lease)** — plus rien à faire de ce côté. Restent :
- `template-presets` : ne le passe pas encore — son propre appel à `compose_image()` cassera (décalage d'arguments positionnels) une fois #13 mergée, à corriger en même temps que la fusion de sa signature avec celle de `longitudinal-section` (zone 2 ci-dessus). Pas encore fait.
- `split-view-export`/`blender-reference-script` : `export_split_views()` a le même genre d'appel à `_draw_rib()` avec l'ancienne signature (voir le bug silencieux n°1 du RETEX plus bas) — la correction de ce point (le "point 3" discuté le 2026-07-14 : exporter la coupe longitudinale comme image séparée) doit de toute façon faire transiter `axis_cfg` jusqu'à `export_split_views()`, donc ce chantier et #13 sont liés. Pas encore fait — prochain chantier.

Ce n'était pas un problème avant #13 puisque `axis_cfg` n'existait nulle part dans `compose_image()`.

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

7. **`fix-compositor-axis-hardcoding` (#13)** : indépendante, sans risque, peut être mergée n'importe quand (aucune des autres branches ne touche `_draw_rib`/`_rib_used_bbox` au-delà des deux appels déjà existants dans `compose_image()`). **Fait le 2026-07-14** : `longitudinal-section` rebasée dessus (un seul conflit à résoudre, retiré la copie redondante du correctif, testé en conditions réelles, poussée en force-with-lease). `blender-reference-script` n'avait pas eu besoin d'être rebasée dessus tant qu'elle partait de `split-view-export` seule (aucun conflit avec #13). **`split-view-export` a depuis été rebasée sur `longitudinal-section` (donc sur #13 aussi, transitivement) et `blender-reference-script` a suivi par-dessus (2026-07-14, voir "Prochain chantier" ci-dessous).**

**Prochain chantier (en cours)** : implémenter les 4 manques identifiés le 2026-07-14 (marqueur longitudinal absent des images individuelles de `split-view-export`, pas de labels S1/S2 sur les traits croisés de la coupe longitudinale, coupe longitudinale absente du ZIP, absente du script Blender) — le point clé est de faire transiter `axis_cfg` jusqu'à `export_split_views()`, ce qui débloque presque tout le reste. Une fois fait, remettre à jour `test-full-integration` pour valider l'ensemble.

## Points à surveiller

- Ne jamais laisser le mainteneur tomber sur un de ces conflits via le bouton merge de GitHub — toujours rebaser proactivement soi-même en amont.
- Avant d'ouvrir une nouvelle PR, revérifier ce fichier : si une branche dont elle dépend a été mergée entre-temps, la rebase d'abord.
- Ce fichier est à mettre à jour à la main (ou à me redemander de le faire) à chaque fois qu'une PR est mergée en amont ou qu'une nouvelle branche est créée.

## RETEX : test d'intégration complet (2026-07-14)

Objectif : valider que la roadmap ci-dessus tient la route, et obtenir une version locale complète simulant "tout accepté" (les 6 PR ouvertes + `template-presets` + `split-view-export` + `blender-reference-script` + `longitudinal-section`).

### Étapes suivies

Branche jetable `test-full-integration` créée depuis `main`, jamais poussée, fusion une branche à la fois dans l'ordre recommandé ci-dessus :

1. `fix-part-count-on-individual-toggle` (#5)
2. `fix-obj-ngon-triangulation` (#12)
3. `fix-hidden-line-depth-eps-scale` (#7)
4. `three-js-preview` (#3)
5. `section-markers` (#2)
6. `fix-rib-marker-position` (#11)
7. `split-view-export`
8. `blender-reference-script`
9. `longitudinal-section`
10. `template-presets`

Après chaque étape sensible (les points 5 à 10), vérification que l'appli s'importe encore (`python -c "import app"`) avant de continuer. Test complet en conditions réelles (serveur lancé, génération via l'API) une fois toutes les branches fusionnées.

### Intégrations réussies sans aucun conflit

Les étapes 1 à 4 et l'étape 8 (`blender-reference-script`, puisqu'elle part directement de `split-view-export` déjà fusionnée) se sont fusionnées automatiquement, sans aucune intervention manuelle.

### Adaptations nécessaires pour les collisions prévues

**Zone 1 : la ligne `fracs_for_view`.** Rencontrée deux fois, comme prévu :
- Entre `section-markers` et `fix-rib-marker-position` : combiné en gardant la structure en tuples `(frac, num)` de `section-markers`, avec la formule de correction de padding de `fix-rib-marker-position` appliquée au premier élément du tuple.
- Entre le résultat ci-dessus et `split-view-export` : `split-view-export` avait sa propre version déjà combinée, mais sans respecter la préférence `label_sections` (elle numérotait toujours en dur). Résolution : garder la version qui respecte la préférence, plus complète.
- Et une troisième fois avec `longitudinal-section`, qui ajoutait la logique de fusion `x_fracs`/`y_fracs` pour son propre marqueur de coupe centrale. Ici, une vraie erreur aurait été possible : la logique de `longitudinal-section` ajoutait `[0.5]` (un float brut) à une liste censée contenir des tuples `(frac, num)`, ce qui aurait cassé le dépaquetage `for frac, num in ...` dans `_draw_view`. Corrigé en ajoutant `(0.5, None)` à la place.

**Zone 2 : la signature de `compose_image()`.** Entre `longitudinal-section` (`axis_cfg`, `longitudinal_segments`) et `template-presets` (`template`) : simple ajout des deux nouveaux paramètres côte à côte, aucune vraie difficulté.

### Les deux bugs silencieux détectés

1. **Confirmé et corrigé** : `export_split_views()` (ajoutée par `split-view-export`) appelait encore `_draw_rib()` avec l'ancienne signature à 4 arguments positionnels (`ax, segs, ppm, bbox, line_color`), alors que `longitudinal-section` avait rendu `h_idx`/`v_idx` obligatoires dans cette fonction. Sans correction, `line_color` (une chaîne de caractères) aurait été passé à la place de `h_idx` (un indice d'axe), ce qui aurait planté ou produit un rendu incohérent. **Aucun conflit git ne l'a signalé**, puisque cette ligne précise n'avait été modifiée par aucune des deux branches — seule sa dépendance (la signature de la fonction appelée) avait changé. Corrigé a minima en passant `h_idx=0, v_idx=1` en dur (comportement identique à avant la généralisation).

2. **Limitation préexistante découverte, non corrigée (hors périmètre)** : en creusant le point 1, `export_split_views()` s'est révélée avoir le même défaut que `compose_image()` avait avant sa propre correction (par `longitudinal-section`) — elle suppose en dur que les indices d'axes 0/1 sont les bons partout dans son propre code (voir son calcul de bbox pour les sections, lignes ~713-714), sans jamais consulter `axis_cfg`. Elle n'a jamais reçu le même correctif. Concrètement : pour un modèle avec `up_axis` différent de la config par défaut (Y), les images de sections individuelles exportées par `split-view-export`/`blender-reference-script` seraient mal projetées, alors que l'image composite principale, elle, serait déjà correcte. À corriger dans un futur ticket dédié (threading `axis_cfg` à travers `export_split_views`), pas traité ici pour rester dans le périmètre du test d'intégration.

### Verdict

La roadmap de merge ci-dessus est validée : les points de friction sont exactement ceux identifiés à l'avance, aucune surprise structurelle. Le seul imprévu (le bug silencieux n°1) est le genre de problème qu'un simple `git merge` sans tests ne peut pas attraper puisqu'aucun conflit n'est levé — bon rappel de toujours tester l'appli réellement après une fusion, pas seulement vérifier l'absence de conflits.
