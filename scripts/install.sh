#!/usr/bin/env bash
# install.sh
#
# Interactive installer for fmg-retrieve-oos on Debian/Ubuntu. Run this once
# after cloning the repo. It asks a few questions and sets up either the CLI
# (systemd timer), the web UI (systemd service), or both - no manual file
# editing required.
#
# Usage: sudo ./scripts/install.sh

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Ce script doit être lancé avec sudo : sudo ./scripts/install.sh" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="/etc/fmg-retrieve-oos"
LOG_DIR="/var/log/fmg-retrieve-oos"
SYSTEMD_DIR="/etc/systemd/system"

echo "=== fmg-retrieve-oos - installation ==="
echo "Dépôt détecté dans : $REPO_DIR"
echo

echo "Quel mode veux-tu installer ?"
echo "  1) CLI seul (cron/systemd timer, pas d'interface)"
echo "  2) Interface web (page dans le navigateur, config + suivi + planification)"
echo "  3) Les deux"
read -rp "Choix [1/2/3] : " MODE

mkdir -p "$CONFIG_DIR" "$LOG_DIR"

# ---- CLI setup ---------------------------------------------------------
if [ "$MODE" = "1" ] || [ "$MODE" = "3" ]; then
  echo
  echo "--- Configuration CLI ---"
  read -rp "Host/IP du FortiManager : " FMG_HOST
  read -rp "ADOM à scanner (ex: BACKUP) : " FMG_ADOM
  read -rp "Utilisateur API FortiManager : " FMG_USER
  read -rsp "Mot de passe de ce compte API : " FMG_PASS
  echo

  cat > "$CONFIG_DIR/fmg.env" <<EOF
FMG_HOST=$FMG_HOST
FMG_ADOM=$FMG_ADOM
FMG_USER=$FMG_USER
EOF

  printf '%s' "$FMG_PASS" > "$CONFIG_DIR/fmg.passwd"
  chmod 600 "$CONFIG_DIR/fmg.passwd" "$CONFIG_DIR/fmg.env"
  unset FMG_PASS

  chmod +x "$REPO_DIR/scripts/fmg_retrieve_oos.py"

  sed -e "s#/opt/fmg-retrieve-oos#$REPO_DIR#g" -e "/^User=/d" -e "/^Group=/d" \
    "$REPO_DIR/systemd/fmg-retrieve-oos.service" > "$SYSTEMD_DIR/fmg-retrieve-oos.service"
  cp "$REPO_DIR/systemd/fmg-retrieve-oos.timer" "$SYSTEMD_DIR/fmg-retrieve-oos.timer"

  systemctl daemon-reload
  systemctl enable --now fmg-retrieve-oos.timer

  echo
  echo "CLI installé. Test immédiat (dry-run, ne déclenche rien) :"
  echo "  sudo env FMG_PASSWORD_FILE=$CONFIG_DIR/fmg.passwd \\"
  echo "    $REPO_DIR/scripts/fmg_retrieve_oos.py --host $FMG_HOST --adom $FMG_ADOM --user $FMG_USER --dry-run"
  echo "Scan périodique actif toutes les 15 min (systemctl status fmg-retrieve-oos.timer)."
fi

# ---- Web UI setup -------------------------------------------------------
if [ "$MODE" = "2" ] || [ "$MODE" = "3" ]; then
  echo
  echo "--- Configuration interface web ---"

  if ! python3 -c "import flask" 2>/dev/null; then
    echo "Installation de python3-flask..."
    apt-get update -qq && apt-get install -y python3-flask
  fi

  read -rp "Login pour accéder à la page (WEBUI_USERNAME) [admin] : " WEBUI_USER
  WEBUI_USER=${WEBUI_USER:-admin}
  read -rsp "Mot de passe pour accéder à la page (WEBUI_PASSWORD) : " WEBUI_PASS
  echo
  read -rp "Port d'écoute local [8877] : " WEBUI_PORT
  WEBUI_PORT=${WEBUI_PORT:-8877}

  cat > "$CONFIG_DIR/webui.env" <<EOF
WEBUI_USERNAME=$WEBUI_USER
WEBUI_PASSWORD=$WEBUI_PASS
FMG_WEBUI_CONFIG_DIR=$CONFIG_DIR
FMG_WEBUI_HOST=127.0.0.1
FMG_WEBUI_PORT=$WEBUI_PORT
EOF
  chmod 600 "$CONFIG_DIR/webui.env"
  unset WEBUI_PASS

  sed -e "s#/opt/fmg-retrieve-oos/webui#$REPO_DIR/webui#g" \
      -e "s#EnvironmentFile=/etc/fmg-retrieve-oos/webui.env#EnvironmentFile=$CONFIG_DIR/webui.env#g" \
      -e "/^User=/d" -e "/^Group=/d" \
      "$REPO_DIR/systemd/fmg-webui.service" > "$SYSTEMD_DIR/fmg-webui.service"

  systemctl daemon-reload
  systemctl enable --now fmg-webui.service

  echo
  echo "Interface web installée et démarrée sur 127.0.0.1:$WEBUI_PORT (côté serveur)."
  echo "Depuis TON poste, ouvre un tunnel puis accède à la page :"
  echo "  ssh -L $WEBUI_PORT:127.0.0.1:$WEBUI_PORT $(logname 2>/dev/null || echo utilisateur)@$(hostname -I | awk '{print $1}')"
  echo "  puis http://127.0.0.1:$WEBUI_PORT  (login: $WEBUI_USER)"
  echo "La config FortiManager (host/ADOM/user/password/log/fréquence) se saisit ensuite DANS la page."
fi

echo
echo "=== Terminé ==="
