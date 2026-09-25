# Wallet Checker — app Android native

Port de l'outil `wallet_checker` (Python) en vraie app Android (Kivy),
compilée en `.apk` via GitHub Actions. Comme c'est une app native (pas une
page web), il n'y a aucune restriction CORS : les requêtes réseau se
comportent exactement comme dans le script Python d'origine (Solana inclus,
tokens et staking compris, sans clé RPC spéciale).

## Étape unique : compiler l'APK (10-15 minutes, une seule fois)

1. Va sur https://github.com/new et crée un nouveau dépôt (nom libre, ex.
   `wallet-checker-apk`), visibilité **privée** ou publique, peu importe.
   Ne coche aucune case d'initialisation (pas de README/gitignore).
2. Sur la page du dépôt vide, clique **"uploading an existing file"** et
   glisse-dépose **tout le contenu de ce dossier** (`main.py`, `report.py`,
   `pricing.py`, `buildozer.spec`, `providers/`, `.github/`) — garde bien la
   structure des sous-dossiers. Valide (`Commit changes`).
3. Va dans l'onglet **Actions** du dépôt. Une exécution "Build Android APK"
   démarre automatiquement (sinon : bouton "Run workflow"). Elle prend
   10 à 15 minutes la première fois (téléchargement du SDK/NDK Android).
4. Une fois verte (✓), clique dessus, puis en bas de la page clique sur
   **wallet-checker-apk** dans "Artifacts" : ça télécharge un `.zip`
   contenant le fichier `.apk`.
5. Transfère ce `.apk` sur ton téléphone (par mail, Drive, câble USB...),
   ouvre-le avec un gestionnaire de fichiers, et installe-le. Android va
   demander d'autoriser "l'installation depuis cette source" (Chrome ou
   ton gestionnaire de fichiers) la première fois — c'est normal pour toute
   app installée hors Play Store.

Si le build échoue (croix rouge dans Actions), ouvre les logs de l'étape
"Build with Buildozer" et colle-moi l'erreur : c'est presque toujours un
détail de configuration Android (version de NDK, dépendance manquante...)
qu'on corrige en une itération.

## Utilisation

1. Ouvre l'app, appuie sur **Paramètres**.
2. Colle ta liste d'adresses (même format que `addresses.txt` : une adresse
   par ligne, `[Label]` pour grouper, `chaine,adresse` pour forcer la
   chaîne). Optionnel : clé Etherscan / beaconcha.in.
3. **Enregistrer**, puis **Vérifier les wallets**.
4. Le résultat s'affiche à l'écran (même format que le script en ligne de
   commande). **Copier JSON** / **Copier CSV** mettent l'export dans le
   presse-papier (colle-le où tu veux : Notes, Sheets, un fichier...).

La configuration est sauvegardée sur le téléphone (pas besoin de la
recoller à chaque lancement).

## Mettre à jour l'app plus tard

Si on corrige un bug ou ajoute une fonctionnalité : je te redonne les
fichiers modifiés, tu les remplaces dans ton dépôt GitHub (upload à nouveau,
même méthode), Actions recompile automatiquement, tu retélécharges le
nouvel APK et le réinstalles par-dessus (Android garde tes paramètres tant
que le `package.name` dans `buildozer.spec` ne change pas).
