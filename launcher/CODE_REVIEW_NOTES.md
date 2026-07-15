# Revue de code — branche `tauri-launcher` (2026-07-15)

Notes personnelles suite à la revue de code du lanceur Tauri (fusion de toutes les branches + nouveau dossier `launcher/`). Pas destiné à une PR, juste une liste de travail pour reprendre plus tard.

Revue faite avec 8 angles en parallèle (bugs, comportement supprimé, traçage inter-fichiers, réutilisation, simplification, efficacité, altitude architecturale, conventions), chaque constat ci-dessous vérifié directement dans le code (pas juste une hypothèse d'agent).

## 1. Sécurité — `/reveal-output` non protégé (le plus important)

**Fichier :** `app.py`, route `/reveal-output` (~ligne 526)

Deux problèmes qui se cumulent :
- La route n'est **pas limitée au contexte Tauri**. N'importe quel client HTTP qui atteint `127.0.0.1:5000` peut l'appeler, pas seulement le lanceur (côté front, seul le bouton est caché derrière `is_tauri`, mais l'endpoint lui-même reste ouvert).
- Le garde-fou anti-évasion de dossier est un simple test de préfixe de chaîne :
  ```python
  if not abs_path.startswith(os.path.normpath(OUTPUT_DIR)):
  ```
  Sans séparateur à la fin, ça matche aussi un dossier voisin dont le nom commence juste par `outputs` (ex: `outputs_backup`, `outputs2`).

**Impact :** un chemin construit intelligemment peut faire exécuter `explorer`/`open`/`xdg-open` sur un fichier hors du dossier `outputs/`.

**Piste de correctif :** comparer avec `OUTPUT_DIR + os.sep` (ou `os.path.commonpath`), et gater la route elle-même derrière `IS_TAURI` (retourner 404 sinon).

## 2. `find_python()` accepte un Python cassé (Rust)

**Fichier :** `launcher/src-tauri/src/lib.rs`, ligne ~50

```rust
if c.status().is_ok() {
    return Ok(cmd.to_string());
}
```

`status()` retourne `Ok` dès que le process a pu être lancé, **sans regarder son code de sortie**. Sur Windows sans vrai Python installé, le stub Microsoft Store se lance très bien mais échoue avec un code non nul — il serait accepté à tort.

**Piste de correctif :** `c.status().map(|s| s.success()).unwrap_or(false)`.

## 3. Lancement de Flask silencieux en cas d'échec

**Fichiers :** `launcher/src-tauri/src/lib.rs` (`launch_flask`, ligne ~125) + `launcher/src/main.js` (`waitForFlask`, ligne ~28)

`launch_flask()` redirige stdout/stderr de Flask vers `Stdio::null()`. Si Flask plante au démarrage (port déjà pris, exception non attrapée), le process meurt silencieusement. Côté JS, `waitForFlask()` boucle indéfiniment sans timeout ni message d'erreur. Résultat : le lanceur reste bloqué sur "Starting app..." pour toujours, sans aucun indice — problématique vu que l'app cible justement des utilisateurs non-techniques.

**Piste de correctif :** capturer stderr de Flask (même juste les dernières lignes en cas de code de sortie non nul), et ajouter un timeout côté JS avec un message d'erreur explicite.

## 4. `export_split_views()` n'est pas à jour avec `compose_image()`

**Fichier :** `compositor.py`, ligne ~719

Deux soucis dans la même fonction, découverts en comparant avec `compose_image()` :
- Position du marqueur de coupe calculée avec une formule différente : `content_span / max_dim` ici, contre `content_span / padded_span` dans `compose_image()`. Ce ne sont pas la même valeur en général → le marqueur peut apparaître à une position différente entre l'image composite et les PNG individuels exportés (et donc aussi sur les plans Blender générés à partir de ces PNG).
- Aucun paramètre `label_sections`/`show_chrome` : les numéros S1-S8 s'affichent toujours sur les exports individuels, même quand la préférence est décochée. (Lié au point qu'on avait déjà repéré et mis de côté pour les panneaux de coupe — ici c'est la même chose mais sur les images de vues individuelles.)

**Piste de correctif :** faire de `export_split_views()` un vrai appelant cohérent avec `compose_image()` plutôt que deux copies indépendantes de la même logique — au minimum, threader `label_sections` et vérifier que les deux formules de position sont censées être équivalentes ou les unifier.

## 5. Commande Rust `reveal_output` jamais appelée (mort-code)

**Fichiers :** `launcher/src-tauri/src/lib.rs` (ligne ~145) vs `templates/index.html` (ligne ~582)

La commande Tauri `reveal_output` (utilise `tauri-plugin-opener`, déjà en dépendance) est bien enregistrée, mais rien ne l'appelle — vérifié par grep sur tous les `invoke(...)` de `main.js`. Le vrai chemin utilisé passe par `fetch('/reveal-output', ...)` côté Flask (voir point 1). C'est probablement un reliquat d'une itération précédente (l'historique de commits montre plusieurs tentatives successives pour régler les téléchargements dans la WebView).

**Piste de correctif :** soit supprimer la commande Rust (et la dépendance `tauri-plugin-opener` si elle ne sert qu'à ça), soit basculer dessus et supprimer l'implémentation Python en double (qui, elle, a le bug de sécurité du point 1).

## 6. Résolution du bundle macOS probablement fausse (non testé)

**Fichier :** `launcher/src-tauri/src/lib.rs`, ligne ~16

`find_ortho_root()` cherche `exe_dir/resources/app.py`, ce qui correspond à la structure Windows/Linux de Tauri mais pas à un vrai bundle `.app` macOS (`Contents/Resources` est un frère de `Contents/MacOS`, pas un sous-dossier). Pas vérifié sur un vrai Mac, à tester avant de packager pour cette plateforme.

## 7. URL du serveur Flask codée en dur à 3 endroits

`app.py` (HOST/PORT), `main.js` (`waitForFlask`), `lib.rs` (`navigate_to_flask`) référencent chacun `127.0.0.1:5000` indépendamment. Changer le port dans un seul endroit casse silencieusement les deux autres.

## 8. Emplacement d'installation packagé (à vérifier)

Le lanceur écrit `uploads/`, `outputs/`, `global_prefs.json` et `.deps_installed` directement dans son propre dossier d'installation (même dossier que `app.py`). Si l'installeur place l'app dans un dossier nécessitant les droits admin (Program Files par exemple), le premier lancement échouerait pour un utilisateur non-admin. À vérifier une fois qu'un vrai build packagé existe.

## 9. Fichiers temporaires non nettoyés en session longue

**Fichier :** `app.py`, `upload()` (~ligne 227)

Le nettoyage du fichier uploadé temporaire ne se fait plus qu'en cas d'échec du parsing, plus systématiquement. Comme le lanceur Tauri garde Flask actif tout le long d'une session (contrairement à un lancement `python app.py` à la demande), plusieurs uploads successifs dans la même session accumulent des fichiers sans être nettoyés avant fermeture complète de l'app.

---

*Aucun de ces points n'a été corrigé, cette revue est juste une liste de travail. À reprendre quand il y a de l'énergie pour ça.*
