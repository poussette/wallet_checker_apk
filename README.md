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

## LP tokens (MultiversX)

Les LP tokens des DEX autres que xExchange n'ont pas de prix public. L'outil les valorise en lisant le **contrat de la pool** (requête `vm-values/query` sur une passerelle MultiversX) : `prix du LP = valeur des réserves ÷ quantité totale de LP`.

- **Nœud personnalisé** : `--mvx-gateway https://mon.noeud` (CLI), variable `MULTIVERSX_GATEWAY_URL`, ou le champ « Gateway API MultiversX » de l’app. Vide = passerelle publique `https://gateway.multiversx.com`. Seul `https://` est accepté. `--no-lp` (ou la case de l'app) désactive la fonction.
- **Trouver la pool** : l'émetteur d'un LP est souvent un routeur/une factory, pas la pool. On interroge donc `/tokens/<LP>/roles` : le contrat qui détient les droits de mint/burn du LP est la pool (puis l'émetteur en dernier recours). Aucun code propre à un DEX pour cette étape.
- **Lire la pool** : un contrat ne publie pas son ABI. Chaque adaptateur (`ADAPTERS` dans `providers/lp.py`) liste des *noms de vues candidats* : `xexchange-pair`, `jex-pair`, `onedex` (un seul contrat, vues indexées par un id de paire), `list-pool` (AshSwap et pools à liste de jetons ; sans vue de réserves, les soldes du contrat servent de réserves), `jex-stable` (pools stables JEX : jetons lus dans `getStatus`, réserves = soldes du contrat). Pour les pools stables, le prix obtenu doit **concorder (±12 %) avec le `getVirtualPrice` publié par la pool**, sinon la ligne reste non valorisée.
- **Garde-fous** : un résultat n'est retenu que si la pool **nomme elle-même ce LP** (obligatoire), chaque réserve est ≤ au solde réel du contrat, et la quantité totale de la pool concorde avec `minted - burnt`. Une mauvaise hypothèse donne « non valorisé », jamais une valeur fausse.
- **Valorisation** : si tous les jetons de la pool ont un prix, on additionne ; si un seul des deux (pool 50/50 à produit constant), on double et la ligne indique « estimation 50/50 » ; pools stables / à plus de 2 jetons : tous les jetons doivent avoir un prix. Les pools stables sans vue de réserves utilisent les soldes du contrat, qui peuvent inclure des frais non distribués : écart de quelques % possible.
- **Un DEX n'est pas reconnu ?** `python lp_probe.py <LP-id ou TICKER>` affiche les contrats candidats et les vues qui répondent. Ajoutez ensuite un dictionnaire à `ADAPTERS` (données seulement), ou envoyez la sortie pour qu'on l'écrive. Non géré à ce jour : la découverte automatique des vues à partir du bytecode.
- **Cache et reprise** : l'outil mémorise, pour chaque LP, quel contrat est la pool et quelle famille de vues la lit (fichier `lp_cache.json`, droits 0600, données publiques uniquement : identifiants de jetons et adresses de contrats, jamais vos adresses ni un prix). Les valeurs sont **recalculées et revérifiées sur la chaîne à chaque lancement**. Les LP illisibles sont ignorés 24 h pour ne pas gaspiller le budget. Si le budget d'une passe est épuisé, jusqu'à 3 passes s'enchaînent dans la même actualisation ; sinon relancez : les LP déjà connus coûtent peu. CLI : `--lp-cache FICHIER` (défaut `~/.wallet_checker/lp_cache.json`, `none` pour désactiver). APK : fichier dans le dossier privé de l'appli.
- **Contrat non vérifié ?** Un code hash inconnu n'est pas une erreur : c'est un contrat que l'outil n'a jamais vu. Comme n'importe qui peut déployer un contrat imitant une pool, l'outil ne s'auto-approuve pas. Deux façons de lever la mention sans me renvoyer chaque hash : (1) `python3 lp_probe.py <LP-id> --trust` après avoir vérifié que c'est bien une pool du DEX : il mémorise le code hash (par adaptateur) dans `lp_cache.json` et toutes les pools au même code deviennent vérifiées (ligne « hash approuvé ») ; (2) le **mode permissif** (CLI `--lp-trust-all`, case dans les paramètres de l'app) : toute pool qui passe les contrôles de cohérence compte comme vérifiée, y compris l'estimation 50/50 (ligne « mode permissif »). Moins sûr, à réserver à un usage personnel.
- **Limites** : budget par passe de 1000 requêtes, 120 s, 80 LP par actualisation ; le prix des jetons de la pool vient toujours de xExchange. **Contrats non reconnus** : n'importe qui peut déployer un contrat qui répond « comme une pool » avec des chiffres inventés. Seuls les contrats dont le *code hash* a été observé sur les vraies pools du DEX (`code_hashes` dans `ADAPTERS`) sont « vérifiés » ; un autre contrat cohérent est valorisé seulement si **tous** ses jetons ont un prix, jamais par doublement 50/50, et la ligne indique « contrat non vérifié ». Une position supérieure à la quantité totale du LP, ou à 50 M$, n'est jamais valorisée. Gardez un œil critique sur ces lignes : c'est une estimation tirée de la chaîne, pas un prix de marché.

## Versions

Chaque fichier porte son numéro (`__version__`) et `providers/version.py` fixe la release. Après avoir copié un patch, vérifiez que rien n'est resté ancien :

- CLI : `python3 main.py --version` liste tous les composants et signale `<-- DIFFERENT` ceux qui ne correspondent pas ; `python3 lp_probe.py <LP>` affiche la même synthèse en tête.
- App : le numéro en haut affiche `(!)` et la barre d'état nomme les fichiers d'une autre version.


## Sécurité

**Ce qui est protégé** (audit v0.5) :
- Toutes les données reçues des API sont traitées comme hostiles (tokens airdrop avec noms piégés, décimales absurdes, montants NaN/inf, réponses géantes) : texte nettoyé (séquences d'échappement, caractères bidi/invisibles), nombres bornés, taille de réponse et nombre de pages plafonnés.
- Les clés API ne fuient plus dans les messages d'erreur, les exports ou le presse-papier (les erreurs `requests` contiennent l'URL complète, donc la clé : elles sont masquées).
- HTTPS uniquement (un RPC personnalisé en `http://` est ignoré, hors `localhost`) ; la vérification TLS n'est jamais désactivée ; les redirections vers HTTP sont refusées.
- Les adresses sont validées (même quand la chaîne est forcée) et encodées avant d'entrer dans une URL.
- Les exports CSV neutralisent l'injection de formules (`=`, `+`, `-`, `@`) ; les fichiers de sortie du CLI sont créés en `0600`.
- Un token dont la valeur dépasse 1 milliard de $ est considéré comme un artefact de prix et laissé non valorisé.

**À savoir (risques résiduels)** :
- Ta liste d'adresses et tes clés sont stockées en clair dans le stockage privé de l'app (isolé des autres apps, hors sauvegardes). Un téléphone rooté ou déverrouillé y accède.
- Les services tiers (Blockstream, CoinGecko, RPC publics...) voient tes adresses et ton IP. Utilise ton propre RPC si c'est un sujet.
- Un faux token airdroppé dans un pool très peu liquide peut afficher une valeur gonflée mais < 1 Md$ : méfie-toi des lignes de tokens inconnus.
- L'APK est signé avec une clé *debug* générée à chaque build : Android considère chaque build comme un éditeur différent (désinstalle avant de réinstaller). Ne distribue pas ce fichier.
- Ne commite jamais ta vraie liste : `addresses.txt`, `wallets*.txt` et les exports sont dans `.gitignore`. Vérifie avec `git ls-files`.

## Affichage des wallets (v0.9.3)

- Chaque label affiche, sous son total, le **nombre de positions par type** (esdt, nft, coin…).
- En ouvrant un wallet, une **synthèse par type** (nombre + valorisation totale) apparaît en tête de liste.
- Les positions **sans valeur connue ou < 1 centime** sont masquées par défaut ; le bouton en bas de liste (« N position(s) masquée(s) … Afficher ») les montre. À la réouverture du wallet elles sont de nouveau masquées. (Les anciennes options des Paramètres ont été supprimées.)
- À l'ouverture d'un label, les wallets sont triés par valorisation totale décroissante.
