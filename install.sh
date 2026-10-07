#!/usr/bin/env bash
# install.sh — instalación de un solo paso para YouTube Stream Monitor API
#
# Uso:
#   git clone <url-del-repo> /opt/yt-monitor
#   cd /opt/yt-monitor
#   sudo ./install.sh [dominio]
#
# [dominio] es opcional — por defecto usa "yt-monitor.local".
# Ejemplo: sudo ./install.sh mi-raspberry.local
#
# Después de esto, solo falta:
#   1. Rellenar .env (ya se crea desde .env.example si no existía)
#   2. (Opcional) Copiar client_secret.json aquí y correr generate_oauth_token.py
#   3. sudo systemctl start yt-monitor
#
# Es seguro volver a ejecutarlo tras un "git pull" — no toca el .env si ya existe.

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Este script necesita sudo. Ejecuta: sudo ./install.sh"
    exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="ytmonitor"
DOMAIN="${1:-yt-monitor.local}"

echo "==> Carpeta de la app: $APP_DIR"
echo "==> Dominio/CN del certificado: $DOMAIN"
echo ""

# 1. Paquetes del sistema
echo "==> Instalando paquetes del sistema..."
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip nginx openssl

# 2. Usuario de servicio (sin privilegios, sin shell)
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -s /usr/sbin/nologin "$SERVICE_USER"
    echo "==> Usuario de servicio '$SERVICE_USER' creado"
else
    echo "==> Usuario de servicio '$SERVICE_USER' ya existía"
fi

# 3. Carpeta de datos (token OAuth, etc.)
mkdir -p "$APP_DIR/data"

# 4. .env desde la plantilla, solo si no existe ya (no pisa configuración existente)
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "==> .env creado desde .env.example"
    NEEDS_ENV_EDIT=1
else
    echo "==> .env ya existía, no se toca"
    NEEDS_ENV_EDIT=0
fi

# 5. Entorno virtual + dependencias
echo "==> Instalando dependencias de Python (puede tardar 1-2 min)..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip -q
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

# 6. Certificado HTTPS autofirmado, solo si no existe ya
mkdir -p /etc/nginx/certs
if [ ! -f /etc/nginx/certs/yt-monitor.crt ]; then
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
      -keyout /etc/nginx/certs/yt-monitor.key \
      -out /etc/nginx/certs/yt-monitor.crt \
      -subj "/CN=$DOMAIN" \
      -addext "subjectAltName=DNS:$DOMAIN" 2>/dev/null
    echo "==> Certificado autofirmado generado para $DOMAIN"
else
    echo "==> Certificado ya existía en /etc/nginx/certs/, no se toca"
fi

# 7. nginx: genera la config real desde la plantilla
sed "s/{{DOMAIN}}/$DOMAIN/g" "$APP_DIR/deploy/nginx.conf.template" > /etc/nginx/sites-available/yt-monitor
ln -sf /etc/nginx/sites-available/yt-monitor /etc/nginx/sites-enabled/yt-monitor
rm -f /etc/nginx/sites-enabled/default
nginx -t -q
systemctl reload nginx
echo "==> nginx configurado"

# 8. systemd: genera el unit file real desde la plantilla
sed "s|{{APP_DIR}}|$APP_DIR|g" "$APP_DIR/deploy/yt-monitor.service.template" > /etc/systemd/system/yt-monitor.service
systemctl daemon-reload
systemctl enable -q yt-monitor
echo "==> Servicio systemd instalado y habilitado (arrancará solo al reiniciar)"

# 9. Permisos — SOLO lo que necesita el servicio, no todo el repo.
# Así "git pull" / "git push" siguen funcionando con tu usuario normal,
# el .git y los .py no cambian de dueño.
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/data" "$APP_DIR/venv" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
[ -f "$APP_DIR/client_secret.json" ] && chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/client_secret.json" && chmod 600 "$APP_DIR/client_secret.json"

echo ""
echo "✅ Instalación completa."
echo ""
if [ "$NEEDS_ENV_EDIT" -eq 1 ]; then
    echo "SIGUIENTE PASO (obligatorio):"
    echo "  1. nano $APP_DIR/.env          (rellena API Key, Channel ID y tokens)"
else
    echo "SIGUIENTE PASO:"
fi
echo "  2. (Opcional, para /api/streams/health) copia tu client_secret.json aquí y ejecuta:"
echo "     sudo -u $SERVICE_USER $APP_DIR/venv/bin/python3 $APP_DIR/generate_oauth_token.py"
echo "  3. sudo systemctl start yt-monitor"
echo "  4. Apunta $DOMAIN a la IP de este servidor en /etc/hosts de cada cliente"
echo "  5. Prueba: curl --cacert /etc/nginx/certs/yt-monitor.crt https://$DOMAIN/api/health"
