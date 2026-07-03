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

## Installation

```bash
sudo install -m 755 scripts/fmg_retrieve_oos.py /usr/local/bin/fmg_retrieve_oos.py
sudo mkdir -p /etc/fmg-retrieve-oos
sudo cp config/fmg.env.example /etc/fmg-retrieve-oos/fmg.env
sudo vi /etc/fmg-retrieve-oos/fmg.env        # renseigner FMG_HOST / FMG_ADOM / FMG_USER

echo -n 'le-mot-de-passe' | sudo tee /etc/fmg-retrieve-oos/fmg.passwd >/dev/null
sudo chmod 600 /etc/fmg-retrieve-oos/fmg.passwd
sudo chown fmg-retrieve:fmg-retrieve /etc/fmg-retrieve-oos/fmg.passwd

# fichier de log par défaut : à créer avec les droits de l'utilisateur qui lance le script
sudo touch /var/log/fmg-retrieve-oos.log
sudo chown fmg-retrieve:fmg-retrieve /var/log/fmg-retrieve-oos.log
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

**Emplacement du fichier de log** : par défaut `/var/log/fmg-retrieve-oos.log`
(constante `DEFAULT_LOG_FILE` en tête du script), en plus de la sortie
stdout (donc aussi visible dans `journalctl -u fmg-retrieve-oos.service`
si lancé via le timer systemd). Change avec `--log-file /autre/chemin.log`,
ou désactive l'écriture fichier avec `--log-file ""`. Si le chemin n'est
pas inscriptible (droits insuffisants), le script log un warning et continue
sur stdout seul plutôt que d'échouer — pense à créer
`/var/log/fmg-retrieve-oos.log` avec les bons droits pour l'utilisateur qui
exécute le script (voir `chown`/`touch` dans la section systemd ci-dessous).

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

### systemd timer (recommandé)

```bash
sudo cp systemd/fmg-retrieve-oos.service systemd/fmg-retrieve-oos.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fmg-retrieve-oos.timer
```

L'intervalle par défaut est 15 minutes (`OnCalendar=*:0/15` dans le
`.timer`), à ajuster. Logs consultables via `journalctl -u
fmg-retrieve-oos.service` ou dans `/var/log/fmg-retrieve-oos.log`.

### cron (alternative)

```
*/15 * * * * fmg-retrieve  FMG_PASSWORD_FILE=/etc/fmg-retrieve-oos/fmg.passwd /usr/local/bin/fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve
```

## Sécurité

- Compte API dédié, permissions minimales, Trusted Hosts sur le FMG.
- Mot de passe stocké hors du script, fichier 600, jamais en argument CLI.
- TLS vérifié par défaut ; `--insecure` n'est prévu que pour du lab avec
  certificat auto-signé, à éviter en production.
- Le script se déconnecte proprement (`/sys/logout`) même en cas d'erreur.
