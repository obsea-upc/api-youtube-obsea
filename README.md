# YouTube Stream Monitor API

API central en Flask que vigila un canal de YouTube y coordina el chequeo
de salud de varios equipos transmitiendo en paralelo, sin que la cuota
diaria de la YouTube Data API se dispare al añadir más streams.

Este repositorio contiene **solo el servidor**. Los scripts que se
conectan a él (ROS2, YOLO, etc.) viven en otro repositorio aparte.

## Índice

1. [Arquitectura](#arquitectura)
2. [Requisitos previos](#requisitos-previos)
3. [Instalación](#instalación)
4. [Configuración](#configuración)
5. [Arranque](#arranque)
6. [Verificación](#verificación)
7. [Endpoints de la API](#endpoints-de-la-api)
8. [Actualizar tras un cambio](#actualizar-tras-un-cambio)
9. [Solución de problemas](#solución-de-problemas)

---

## Arquitectura

```
Clientes (Jetson, PC, etc.) ──POST /api/register──▶  Esta API (Flask + nginx)
                                                            │
                                                            │ 1 llamada agrupada
                                                            │ (liveStreams.list +
                                                            │  liveBroadcasts.list,
                                                            │  hasta 50 IDs a la vez)
                                                            ▼
                                                   YouTube Data API v3
                                                (API Key para datos públicos,
                                                 OAuth readonly para status)
```

Cada cliente se registra una vez (`POST /api/register`) y luego solo
pregunta a esta API (`GET /api/streams/health`), que hace **una sola
llamada agrupada** a YouTube por ciclo cubriendo a todos los nodos
registrados. El costo de cuota no crece con el número de streams.

### Estructura de archivos

```
.
├── app.py                   # Endpoints Flask
├── auth.py                  # Tokens bearer + rate limiting
├── youtube_monitor.py       # Escaneo del canal + chequeos agrupados
├── generate_oauth_token.py  # Setup único del OAuth de solo lectura
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md                 # Este archivo
├── SUBIR_A_GITHUB.md          # Cómo publicar este repo de forma segura
└── deploy/
    ├── nginx.conf
    └── yt-monitor.service
```

---

## Requisitos previos

- Un servidor Linux (Debian/Ubuntu; esta guía usa Raspberry Pi OS como
  ejemplo, pero los comandos son iguales en cualquier Debian/Ubuntu).
- Acceso por SSH con permisos de `sudo`.
- Tu **API Key** de YouTube (Google Cloud Console → Credentials).
- El **ID de tu canal** de YouTube (empieza por `UC...`).
- Un `client_secret.json` de Google Cloud (tipo "Desktop app", con la
  YouTube Data API v3 habilitada) — hace falta para el paso de OAuth.

---

## Instalación

### 1. Prepara el sistema

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-venv python3-pip nginx openssl git
```

### 2. Crea un usuario dedicado para el servicio

```bash
sudo useradd -r -s /usr/sbin/nologin ytmonitor
```

Por seguridad, la API no corre con tu usuario personal ni como `root`.

### 3. Crea la estructura de carpetas

```bash
sudo mkdir -p /opt/yt-monitor/deploy
sudo mkdir -p /opt/yt-monitor/data
```

### 4. Copia los archivos de este repositorio al servidor

Desde tu PC, dentro de la carpeta del repo clonado:

```bash
scp app.py auth.py youtube_monitor.py generate_oauth_token.py \
    requirements.txt .env.example \
    usuario@ip-del-servidor:/tmp/

scp deploy/nginx.conf deploy/yt-monitor.service usuario@ip-del-servidor:/tmp/

# El client_secret.json NO va en el repo (está en .gitignore) — cópialo aparte:
scp /ruta/local/client_secret.json usuario@ip-del-servidor:/tmp/
```

En el servidor:

```bash
sudo mv /tmp/app.py /tmp/auth.py /tmp/youtube_monitor.py \
        /tmp/generate_oauth_token.py /tmp/requirements.txt \
        /tmp/.env.example /tmp/client_secret.json /opt/yt-monitor/
sudo mv /tmp/nginx.conf /tmp/yt-monitor.service /opt/yt-monitor/deploy/
```

### 5. Entorno virtual e instalación de dependencias

```bash
cd /opt/yt-monitor
sudo python3 -m venv venv
sudo venv/bin/pip install --upgrade pip
sudo venv/bin/pip install -r requirements.txt
```

---

## Configuración

### 1. Genera los tokens de acceso (uno por cada cliente futuro)

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Repite una vez por cada dispositivo que vaya a consumir la API.

### 2. Crea el archivo `.env`

```bash
sudo cp /opt/yt-monitor/.env.example /opt/yt-monitor/.env
sudo nano /opt/yt-monitor/.env
```

Rellena:

```bash
YOUTUBE_API_KEY=tu_api_key_de_google_cloud
YOUTUBE_CHANNEL_ID=UCxxxxxxxxxxxxxxxxxxxxxx
POLL_INTERVAL=60

YOUTUBE_OAUTH_CLIENT_SECRETS=/opt/yt-monitor/client_secret.json
YOUTUBE_OAUTH_TOKEN_PATH=/opt/yt-monitor/data/oauth_readonly_token.pickle

API_TOKENS=nombre-cliente-1:TOKEN_GENERADO_1,nombre-cliente-2:TOKEN_GENERADO_2

BEHIND_PROXY=true
RATE_LIMIT_REQUESTS=60
RATE_LIMIT_WINDOW=60
EXPENSIVE_LIMIT_REQUESTS=5
EXPENSIVE_LIMIT_WINDOW=3600
REGISTRY_TTL=900
```

`API_TOKENS` lleva el formato `nombre:token,nombre2:token2`, sin espacios.

### 3. Ajusta permisos

```bash
sudo chown -R ytmonitor:ytmonitor /opt/yt-monitor
sudo chmod 600 /opt/yt-monitor/.env
sudo chmod 600 /opt/yt-monitor/client_secret.json
sudo ls -l /opt/yt-monitor/.env   # debe decir -rw------- ytmonitor ytmonitor
```

### 4. Autoriza el acceso de solo lectura a YouTube (una sola vez)

Esto permite a `/api/streams/health` leer el estado real de las
transmisiones (`liveStreams.list`/`liveBroadcasts.list` exigen OAuth —
una API Key sola no alcanza, aunque solo sea para leer).

```bash
cd /opt/yt-monitor
sudo -u ytmonitor venv/bin/python3 generate_oauth_token.py \
    --client-secrets /opt/yt-monitor/client_secret.json \
    --token-out /opt/yt-monitor/data/oauth_readonly_token.pickle
```

El script imprime una URL y queda esperando. Como el servidor normalmente
no tiene navegador, abre un túnel SSH **desde tu PC, en otra terminal**:

```bash
ssh -L 8080:localhost:8080 usuario@ip-del-servidor
```

Deja esa terminal abierta y pega la URL en el navegador de tu PC. Tras
autorizar con la cuenta de Google dueña del canal, verás:

```
✅ Token guardado en /opt/yt-monitor/data/oauth_readonly_token.pickle (permisos 600).
```

> Si ya tienes un `token.pickle` que otro script tuyo usa para gestionar el
> canal (crear/borrar transmisiones), puedes reutilizarlo aquí en vez de
> generar uno nuevo — simplemente cópialo a la ruta de
> `YOUTUBE_OAUTH_TOKEN_PATH`. Ahorra el paso del túnel SSH, a cambio de que
> ese token central tiene más permisos de los estrictamente necesarios.

### 5. Genera el certificado HTTPS

```bash
sudo mkdir -p /etc/nginx/certs
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/nginx/certs/yt-monitor.key \
  -out /etc/nginx/certs/yt-monitor.crt \
  -subj "/CN=yt-monitor.local" \
  -addext "subjectAltName=DNS:yt-monitor.local"
```

Cambia `yt-monitor.local` por el nombre que prefieras (o el de tu
servidor, si usas mDNS tipo `nombre-servidor.local`).

### 6. Configura nginx

```bash
sudo cp /opt/yt-monitor/deploy/nginx.conf /etc/nginx/sites-available/yt-monitor
sudo nano /etc/nginx/sites-available/yt-monitor
```

Confirma (aparece dos veces `server_name`):

```nginx
server_name yt-monitor.local;
ssl_certificate     /etc/nginx/certs/yt-monitor.crt;
ssl_certificate_key /etc/nginx/certs/yt-monitor.key;
```

```bash
sudo ln -sf /etc/nginx/sites-available/yt-monitor /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

### 7. Apunta el nombre a la IP del servidor (en cada cliente)

```bash
hostname -I   # en el servidor, para obtener su IP
```

En cada máquina que vaya a hablar con la API (tu PC incluida), añade al
`/etc/hosts` (o `C:\Windows\System32\drivers\etc\hosts` en Windows,
editado como administrador):

```
192.168.x.x   yt-monitor.local
```

Si la IP del servidor puede cambiar, resérvala en tu router (DHCP
reservation) para que no cambie.

---

## Arranque

```bash
sudo cp /opt/yt-monitor/deploy/yt-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable yt-monitor
sudo systemctl start yt-monitor
```

`enable` hace que arranque solo con el servidor; `start` lo arranca ahora.

**Comandos que vas a usar seguido:**

```bash
sudo systemctl status yt-monitor      # ver si está corriendo
sudo systemctl restart yt-monitor     # aplicar cambios del .env
sudo systemctl stop yt-monitor        # parar (se queda parado)
sudo journalctl -u yt-monitor -f      # ver logs en vivo (Ctrl+C para salir)
sudo journalctl -u yt-monitor -n 50   # últimas 50 líneas
```

> `enable`/`disable` controla si arranca solo al reiniciar el servidor.
> `start`/`stop` controla si está corriendo *ahora mismo*. Son
> independientes: si lo paras con `stop` mientras está `enabled`, seguirá
> arrancando solo la próxima vez que reinicies el servidor.

---

## Verificación

```bash
# 1. Health check, sin token -> {"status":"ok"}
curl --cacert /etc/nginx/certs/yt-monitor.crt https://yt-monitor.local/api/health

# 2. Sin token -> 401 (confirma que la seguridad funciona)
curl -i --cacert /etc/nginx/certs/yt-monitor.crt https://yt-monitor.local/api/streams

# 3. Con token -> 200 con datos del canal
curl --cacert /etc/nginx/certs/yt-monitor.crt \
     -H "Authorization: Bearer TU_TOKEN" \
     https://yt-monitor.local/api/streams

# 4. Confirma que el OAuth quedó bien configurado
curl --cacert /etc/nginx/certs/yt-monitor.crt \
     -H "Authorization: Bearer TU_TOKEN" \
     https://yt-monitor.local/api/streams/health
# debe incluir "oauth_ready": true
```

---

## Endpoints de la API

| Método | Ruta | Auth | Gasta cuota | Descripción |
|---|---|---|---|---|
| GET | `/api/health` | No | No | Liveness check |
| GET | `/api/streams` | Token | No (lee caché) | Snapshot del canal: live + upcoming |
| GET | `/api/streams/live` | Token | No | Solo streams en vivo |
| GET | `/api/streams/upcoming` | Token | No | Solo programados |
| POST | `/api/register` | Token | No | Un nodo anuncia su broadcast/stream activo |
| POST | `/api/unregister` | Token | No | Un nodo se da de baja |
| GET | `/api/streams/health` | Token | No | Estado agrupado de todos los nodos registrados |
| POST | `/api/streams/refresh` | Token (límite estricto) | **Sí** | Fuerza un escaneo inmediato del canal |

### Sobre la cuota de la YouTube Data API

- **Descubrimiento del canal**: `channels.list` (1 vez) → `playlistItems.list`
  (1 unidad/ciclo) → `videos.list` (1 unidad/ciclo). No usa `search.list`
  (100 unidades/llamada).
- **Salud de streams registrados**: `liveStreams.list` + `liveBroadcasts.list`,
  hasta 50 IDs por llamada, **1 unidad sin importar cuántos IDs incluya**.
- Con `POLL_INTERVAL=60`: ~4 unidades/ciclo × 1440 ciclos/día ≈ **5,760
  unidades/día**, bien por debajo del límite gratuito de 10,000.

---

## Actualizar tras un cambio

```bash
# Copia el archivo actualizado
scp app.py usuario@ip-del-servidor:/tmp/
# En el servidor:
sudo mv /tmp/app.py /opt/yt-monitor/
sudo chown ytmonitor:ytmonitor /opt/yt-monitor/app.py
sudo systemctl restart yt-monitor
```

Lo mismo para cualquier otro `.py`. Si cambia `requirements.txt`:

```bash
cd /opt/yt-monitor && sudo venv/bin/pip install -r requirements.txt
sudo systemctl restart yt-monitor
```

---

## Solución de problemas

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `curl: SSL certificate problem` | Certificado no coincide o no se copió | Repite el paso 5 de Configuración |
| `401 unauthorized` | Token mal escrito o ausente | Revisa el header `Authorization: Bearer ...` |
| `502 Bad Gateway` | El programa Python no está corriendo | `sudo systemctl status yt-monitor` + `journalctl` |
| No encuentra `yt-monitor.local` | Falta la línea en `/etc/hosts`, o la IP cambió | Repite el paso 7 de Configuración |
| `oauth_ready: false` | Paso 4 de Configuración no completado, o rutas mal en `.env` | Revisa `grep OAUTH /opt/yt-monitor/.env` y que los archivos existan |
| `OAuth de solo lectura no configurado` en el log | Falta alguna línea `YOUTUBE_OAUTH_*` en el `.env` | Añádelas y reinicia |
| `429 too_many_attempts` / `rate_limit_exceeded` | Límite interno superado (no es cuota de YouTube) | Sube `RATE_LIMIT_REQUESTS` en el `.env` si es tráfico legítimo |
| Cambié el `.env` y no pasa nada | systemd solo lo lee al arrancar | `sudo systemctl restart yt-monitor` |
| `quotaExceeded` en los logs | Cuota diaria de YouTube agotada | Se resetea a medianoche hora del Pacífico (~09:00 en España) |
| `Tokens cargados para 0 cliente(s)` | Formato de `API_TOKENS` mal | Debe ser `nombre:token,nombre2:token2`, sin espacios |
