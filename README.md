# fmg-retrieve-oos

Script à exécuter **depuis un serveur Linux** (pas sur le FortiManager) qui
appelle l'API JSON-RPC du FortiManager pour :

1. Lister les FortiGate d'un ADOM donné (typiquement un ADOM de backup)
   via `GET /dvmdb/adom/<adom>/device` et lire leur `conf_status`.
2. Pour chaque device dont `conf_status == outofsync`, déclencher un
   "Retrieve Config" (device -> FortiManager) via
   `EXEC /dvm/cmd/update/device` — l'équivalent API de
   `diagnose test deploymanager reloadconf <oid>`, mais sans SSH ni parsing
   de sortie CLI.

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
```

Le mot de passe n'est **jamais** passé en argument CLI (visible dans `ps`),
uniquement via `--password-file`, `FMG_PASSWORD_FILE` ou `FMG_PASSWORD`.

## Usage manuel

```bash
# Voir ce qui serait retrieve, sans rien déclencher
FMG_PASSWORD_FILE=/etc/fmg-retrieve-oos/fmg.passwd \
  ./scripts/fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve --dry-run

# Retrieve réel des seuls devices désynchronisés
FMG_PASSWORD_FILE=/etc/fmg-retrieve-oos/fmg.passwd \
  ./scripts/fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve

# Idem, en attendant la fin de chaque tâche de retrieve (utile en cron pour avoir un exit code fiable)
./scripts/fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve --wait

# Retrieve de TOUS les devices de l'ADOM, pas seulement les out-of-sync
./scripts/fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve --all
```

Codes de sortie : `0` = OK, `1` = au moins un retrieve en échec/timeout,
`2` = erreur API/connexion/auth, `3` = erreur d'arguments/config.

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
*/15 * * * * fmg-retrieve  FMG_PASSWORD_FILE=/etc/fmg-retrieve-oos/fmg.passwd /usr/local/bin/fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve --wait --log-file /var/log/fmg-retrieve-oos.log
```

## Sécurité

- Compte API dédié, permissions minimales, Trusted Hosts sur le FMG.
- Mot de passe stocké hors du script, fichier 600, jamais en argument CLI.
- TLS vérifié par défaut ; `--insecure` n'est prévu que pour du lab avec
  certificat auto-signé, à éviter en production.
- Le script se déconnecte proprement (`/sys/logout`) même en cas d'erreur.
