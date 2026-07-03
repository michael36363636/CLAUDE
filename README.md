# fmg-retrieve-oos

Script à exécuter **depuis un serveur Linux** (pas sur le FortiManager) qui
appelle l'API JSON-RPC du FortiManager pour :

1. Lister les FortiGate d'un ADOM donné (typiquement un ADOM de backup)
   via `GET /dvmdb/adom/<adom>/device` et lire leur `conf_status`.
2. Pour chaque device dont `conf_status == outofsync` **exactement**,
   déclencher un "Retrieve Config" (device -> FortiManager) via
   `EXEC /dvm/cmd/update/device` — l'équivalent API de
   `diagnose test deploymanager reloadconf <oid>`, mais sans SSH ni parsing
   de sortie CLI. Les devices `insync` ou `unknown` (jamais checké, autre
   statut) ne sont **jamais** retrieve par défaut — seul `--all` force un
   retrieve sur tous les devices, statut par statut.
3. Attendre la fin de chaque tâche de retrieve (par défaut) et écrire un
   rapport clair, sous forme de tableau, dans le log : tous les FGT de
   l'ADOM avec leur statut (SYNC / DESYNC / INCONNU), si un retrieve a été
   déclenché et à quelle heure, et le résultat (SUCCESS / FAILED + raison
   renvoyée par l'API).

Pourquoi pas le script SSH d'origine : celui-ci tourne en local sur le FMG
et parse le texte de `diagnose dvm device list`, dont le format de colonnes
varie selon les builds FortiOS/FMG. L'approche API (`/dvmdb/...`,
`conf_status`) est stable et documentée, et permet de piloter le FMG à
distance depuis un serveur Linux, ce qui correspond au besoin initial.

## Prérequis côté FortiManager

- Un compte admin dédié, à privilèges limités (profil JSON API en lecture
  sur le Device Manager de l'ADOM concerné + droit d'exécuter
  "Retrieve Config"). Ne pas réutiliser un compte admin générique.
- Accès HTTPS (443) atteignable depuis le serveur Linux.
- Idéalement, restreindre la source IP autorisée pour ce compte API
  (System Settings > Administrators > Trusted Hosts).

## Structure du dépôt

```
lib/fmg_common.py       client JSON-RPC FortiManager + logique de scan, partagé par le CLI et le webui
scripts/fmg_retrieve_oos.py   CLI (cron/systemd timer)
webui/app.py             interface web Flask (config + lancement + suivi + planification)
webui/templates/index.html
systemd/                 unités systemd pour les deux modes
config/                  fichiers d'exemple (.env)
```

Le CLI importe `lib/fmg_common.py` par chemin relatif : il faut donc garder
la structure du dépôt intacte (ne pas déplacer juste `fmg_retrieve_oos.py`
tout seul dans `/usr/local/bin`).

## Installation (CLI)

```bash
sudo mkdir -p /opt/fmg-retrieve-oos
sudo cp -r lib scripts config systemd /opt/fmg-retrieve-oos/
sudo chmod +x /opt/fmg-retrieve-oos/scripts/fmg_retrieve_oos.py

sudo mkdir -p /etc/fmg-retrieve-oos
sudo cp config/fmg.env.example /etc/fmg-retrieve-oos/fmg.env
sudo vi /etc/fmg-retrieve-oos/fmg.env        # renseigner FMG_HOST / FMG_ADOM / FMG_USER

echo -n 'le-mot-de-passe' | sudo tee /etc/fmg-retrieve-oos/fmg.passwd >/dev/null
sudo chmod 600 /etc/fmg-retrieve-oos/fmg.passwd
sudo chown fmg-retrieve:fmg-retrieve /etc/fmg-retrieve-oos/fmg.passwd

# dossier de log par défaut : à créer avec les droits de l'utilisateur qui lance le script
sudo mkdir -p /var/log/fmg-retrieve-oos
sudo chown fmg-retrieve:fmg-retrieve /var/log/fmg-retrieve-oos
```

Le mot de passe n'est **jamais** passé en argument CLI (visible dans `ps`),
uniquement via `--password-file`, `FMG_PASSWORD_FILE` ou `FMG_PASSWORD`.

## Usage manuel

```bash
# Voir ce qui serait retrieve, sans rien déclencher
FMG_PASSWORD_FILE=/etc/fmg-retrieve-oos/fmg.passwd \
  ./scripts/fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve --dry-run

# Retrieve réel des seuls devices désynchronisés et joignables (attend la fin de chaque tâche par défaut)
FMG_PASSWORD_FILE=/etc/fmg-retrieve-oos/fmg.passwd \
  ./scripts/fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve

# Fire-and-forget : déclenche les retrieves sans attendre la fin des tâches
./scripts/fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve --no-wait

# Retrieve de TOUS les devices de l'ADOM, pas seulement les out-of-sync
./scripts/fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve --all
```

Codes de sortie : `0` = OK, `1` = au moins un retrieve en échec/timeout,
`2` = erreur API/connexion/auth, `3` = erreur d'arguments/config.

### Rapport / log

**Un fichier par run** : chaque exécution écrit son propre fichier
horodaté dans `--log-dir` (défaut `/var/log/fmg-retrieve-oos`), nommé
`fmg-retrieve-oos_AAAA-MM-JJ_HH-MM-SS.log` — pas de fichier unique qui
grossit indéfiniment. Le nom du fichier créé est aussi affiché en première
ligne du log (utile pour le retrouver depuis `journalctl`). Idem côté
sortie stdout, donc visible dans `journalctl -u fmg-retrieve-oos.service`
si lancé via le timer systemd. Change de dossier avec `--log-dir
/autre/dossier`, ou désactive l'écriture fichier avec `--log-dir ""`. Si
le dossier n'est pas accessible en écriture, le script log un warning et
continue sur stdout seul plutôt que d'échouer.

**Rétention** : à chaque run, les anciens fichiers de plus de
`--log-retention-days` jours (défaut 30) sont automatiquement supprimés du
dossier de log, pour éviter que ça grossisse indéfiniment avec un scan
fréquent. `--log-retention-days 0` désactive la purge.

Les fichiers sont du texte UTF-8 brut, lisibles avec n'importe quel outil
(`cat`, `less -S` pour éviter le retour à la ligne sur le tableau large,
`tail -f` pendant qu'un run est en cours, `grep FAILED *.log` pour
retrouver rapidement les échecs sur plusieurs runs, etc.).

Chaque exécution termine par un tableau récapitulatif (dans stdout et dans
le fichier de log) :

```
NAME      | SN   | IP       | CONNEXION | STATUT  | ACTION    | HEURE               | RESULTAT | RAISON
----------+------+----------+-----------+---------+-----------+---------------------+----------+-----------------------------------------------------------
FGT-PARIS | FGT1 | 10.0.0.1 | UP        | DESYNC  | retrieved | 2026-07-03 12:55:32 | SUCCESS  | Retrieve succeeded
FGT-LYON  | FGT2 | 10.0.0.2 | UP        | SYNC    | skipped   | -                   | -        | conf_status=insync (pas de retrieve)
FGT-NICE  | FGT3 | 10.0.0.3 | INCONNU   | INCONNU | skipped   | -                   | -        | conf_status=unknown (pas de retrieve)
FGT-METZ  | FGT4 | 10.0.0.4 | UP        | DESYNC  | retrieved | 2026-07-03 12:55:32 | FAILED   | device unreachable
FGT-LILLE | FGT5 | 10.0.0.5 | DOWN      | DESYNC  | skipped   | -                   | -        | conn_status=down (FGT injoignable, retrieve non déclenché)
```

- Seuls les FGT `DESYNC` (`conf_status=outofsync`) **et** `CONNEXION=UP`
  sont retrieve. `SYNC`/`INCONNU` (conf_status) et `DOWN`/`INCONNU`
  (connectivité FGT<->FMG) sont toujours listés dans le tableau mais jamais
  traités, même avec `--all` pour la connectivité.
- `HEURE` = heure de déclenchement du retrieve.
- `RESULTAT`/`RAISON` viennent du détail de la tâche FortiManager
  (`/task/task/<id>`), donc reflètent le vrai message d'erreur API en cas
  d'échec (device injoignable en cours de tâche, timeout, etc.).

## Exécution périodique

Deux options indépendantes, pas besoin des deux :
- **CLI + systemd timer/cron** ci-dessous, si tu veux juste un job sans interface.
- **Interface web** (section suivante) avec sa propre case "scan automatique en continu" et
  sa fréquence en minutes, si tu veux une page pour suivre/configurer sans toucher au terminal.

### systemd timer (recommandé pour le CLI)

```bash
sudo cp systemd/fmg-retrieve-oos.service systemd/fmg-retrieve-oos.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fmg-retrieve-oos.timer
```

L'intervalle par défaut est 15 minutes (`OnCalendar=*:0/15` dans le
`.timer`), à ajuster. Logs consultables via `journalctl -u
fmg-retrieve-oos.service`, ou fichier par fichier dans
`/var/log/fmg-retrieve-oos/` (un par run, purgés après 30 jours).

### cron (alternative)

```
*/15 * * * * fmg-retrieve  FMG_PASSWORD_FILE=/etc/fmg-retrieve-oos/fmg.passwd /opt/fmg-retrieve-oos/scripts/fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve
```

## Interface web

Le serveur ciblé (Debian 12, headless, accès SSH uniquement) n'a pas de
bureau graphique : l'interface est donc une petite page web (Flask), servie
en local sur le serveur et consultée depuis un navigateur via un tunnel
SSH — pas d'installation côté poste client.

Elle permet de :
- configurer host/port/ADOM/utilisateur/mot de passe FortiManager, dossier
  de log + rétention, et les options avancées (TLS, timeouts) ;
- lancer un scan à la demande (bouton "Lancer maintenant" ou "Tester -
  dry-run") avec une barre de progression et un log en direct pendant que
  ça tourne ;
- voir le résumé (nb scannés / désync / retrieve lancés / succès / échecs)
  et le tableau détaillé, identique à celui du CLI ;
- activer un scan automatique en continu, en indiquant la fréquence en
  minutes (case à cocher + champ "Fréquence").

### Prérequis

```bash
sudo apt install python3-flask
```

(Flask est le seul paquet à installer en plus de Python 3, déjà présent sur
Debian 12 — pas besoin de `pip`/`venv`.)

### Installation

```bash
sudo mkdir -p /opt/fmg-retrieve-oos
sudo cp -r lib webui /opt/fmg-retrieve-oos/

sudo mkdir -p /etc/fmg-retrieve-oos
sudo cp config/webui.env.example /etc/fmg-retrieve-oos/webui.env
sudo vi /etc/fmg-retrieve-oos/webui.env   # WEBUI_USERNAME / WEBUI_PASSWORD (login de la page, PAS le compte FMG)

sudo cp systemd/fmg-webui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fmg-webui.service
```

La config FortiManager (host/ADOM/user/password/log/fréquence) se
renseigne ensuite **depuis la page elle-même**, pas dans un fichier — elle
est stockée dans `FMG_WEBUI_CONFIG_DIR` (par défaut
`/etc/fmg-retrieve-oos` si défini dans `webui.env`, sinon
`~/.config/fmg-retrieve-oos` de l'utilisateur qui lance le service), avec
le mot de passe FMG dans un fichier séparé en mode 600, comme pour le CLI.

### Accès depuis ton poste

Le service écoute par défaut sur `127.0.0.1:8877` **uniquement** (pas sur
le réseau) — volontaire, vu qu'il peut déclencher des actions sur le FMG.
Depuis ton poste :

```bash
ssh -L 8877:127.0.0.1:8877 utilisateur@debian-server
```

puis ouvrir `http://127.0.0.1:8877` dans ton navigateur. Un login/mot de
passe (Basic Auth, ceux définis dans `webui.env`) est demandé avant tout
accès.

## Comment se fait la connexion à l'API FortiManager

Le client (`lib/fmg_common.py`) parle JSON-RPC en HTTPS, comme le fait la
GUI du FortiManager en interne :

1. **Login** — `POST https://<fmg>/jsonrpc` avec
   `{"method":"exec","params":[{"url":"/sys/login/user","data":{"user":"...","passwd":"..."}}]}`.
   La réponse contient un `session` (token) réutilisé pour tous les appels
   suivants — pas de "clé API" façon FortiGate, juste un compte admin avec
   un profil JSON API activé côté FMG.
2. **Lister les devices de l'ADOM** — `GET /dvmdb/adom/<adom>/device` avec
   les champs `conf_status` (sync FMG<->FGT) et `conn_status` (FGT
   joignable ou non).
3. **Retrieve** — `EXEC /dvm/cmd/update/device` avec `adom`, `device`, et
   `flags: ["create_task","nonblocking"]`, qui renvoie un `task` id.
4. **Suivi de la tâche** — `GET /task/task/<id>` jusqu'à `state: done`,
   avec le détail par device (`line[].err`/`line[].detail`) pour savoir
   pourquoi ça a échoué le cas échéant.
5. **Logout** — `EXEC /sys/logout`, toujours exécuté même en cas d'erreur.

## Sécurité

- Compte API FortiManager dédié, permissions minimales, Trusted Hosts sur
  le FMG.
- Mot de passe FMG stocké hors du script/de la page, fichier 600, jamais
  en argument CLI ni en clair dans le formulaire une fois enregistré.
- TLS vérifié par défaut ; l'option "ignorer le certificat" n'est prévue
  que pour du lab avec certificat auto-signé, à éviter en production.
- Le client se déconnecte proprement (`/sys/logout`) même en cas d'erreur.
- L'interface web a son propre login (Basic Auth, `WEBUI_USERNAME`/
  `WEBUI_PASSWORD`), distinct du compte FortiManager, et n'écoute par
  défaut que sur `127.0.0.1` — accès prévu via tunnel SSH, pas d'exposition
  directe sur le réseau.
