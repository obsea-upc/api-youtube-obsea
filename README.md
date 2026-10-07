# YouTube Stream Monitor API

API central en Flask que vigila un canal de YouTube y coordina el chequeo
de salud de varios equipos transmitiendo en paralelo, sin que la cuota
diaria de la YouTube Data API se dispare al añadir más streams.

Este repositorio contiene **solo el servidor**. Los scripts que se
conectan a él (ROS2, YOLO, etc.) viven en otro repositorio aparte.

## Instalación rápida

```bash
git clone <url-de-este-repo> /opt/yt-monitor
cd /opt/yt-monitor
sudo ./install.sh
nano .env                          # rellena tu API Key, Channel ID y tokens
sudo systemctl start yt-monitor
```

Eso es todo para lo básico (descubrimiento del canal, `/api/streams`).
Si además quieres `/api/streams/health` (el chequeo de salud agrupado que
usan tus scripts de streaming), hace falta un paso más — ver
[OAuth de solo lectura](#oauth-de-solo-lectura-opcional-pero-recomendado)
más abajo.

El instalador (`install.sh`) hace todo lo demás por ti: instala
dependencias del sistema, crea un usuario de servicio sin privilegios,
monta el entorno virtual de Python, genera un certificado HTTPS
autofirmado, configura nginx y systemd. Es seguro volver a ejecutarlo
tras un `git pull` — no toca tu `.env` si ya existe.

## Índice

1. [Instalación rápida](#instalación-rápida)
2. [Arquitectura](#arquitectura)
3. [Requisitos previos](#requisitos-previos)
4. [Configurar `.env`](#configurar-env)
5. [OAuth de solo lectura](#oauth-de-solo-lectura-opcional-pero-recomendado)
6. [Arranque y gestión del servicio](#arranque-y-gestión-del-servicio)
7. [Verificación](#verificación)
8. [Conectar clientes (otros dispositivos)](#conectar-clientes-otros-dispositivos)
9. [Endpoints de la API](#endpoints-de-la-api)
10. [Actualizar](#actualizar)
11. [Solución de problemas](#solución-de-problemas)

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

### Estructura del repositorio

```
.
├── install.sh                   # Instalador de un solo paso
├── app.py                       # Endpoints Flask
├── auth.py                      # Tokens bearer + rate limiting
├── youtube_monitor.py           # Escaneo del canal + chequeos agrupados
├── generate_oauth_token.py      # Setup único del OAuth de solo lectura
├── requirements.txt
├── .env.example                 # Plantilla — install.sh la copia a .env
├── .gitignore
├── README.md                    # Este archivo
├── SUBIR_A_GITHUB.md            # Cómo publicar este repo de forma segura
└── deploy/
    ├── nginx.conf.template       # install.sh la convierte en la config real
    └── yt-monitor.service.template

# Generados por install.sh, NUNCA en git (ver .gitignore):
#   venv/                 entorno virtual de Python
#   data/                 token OAuth
#   .env                  tu configuración real
#   client_secret.json    si lo subes para el paso de OAuth
```

---

## Requisitos previos

- Un servidor Linux Debian/Ubuntu (incluida Raspberry Pi OS) con `sudo`.
- Tu **API Key** de YouTube (Google Cloud Console → Credentials).
- El **ID de tu canal** de YouTube (empieza por `UC...`).
- *(Opcional, para el chequeo de salud)* un `client_secret.json` de Google
  Cloud, tipo "Desktop app", con la YouTube Data API v3 habilitada.

---

## Configurar `.env`

`install.sh` ya te crea el archivo desde `.env.example` — solo edítalo:

```bash
nano /opt/yt-monitor/.env
```

```bash
YOUTUBE_API_KEY=tu_api_key_de_google_cloud
YOUTUBE_CHANNEL_ID=UCxxxxxxxxxxxxxxxxxxxxxx
POLL_INTERVAL=60

API_TOKENS=nombre-cliente-1:TOKEN_GENERADO_1,nombre-cliente-2:TOKEN_GENERADO_2

BEHIND_PROXY=true
RATE_LIMIT_REQUESTS=60
RATE_LIMIT_WINDOW=60
EXPENSIVE_LIMIT_REQUESTS=5
EXPENSIVE_LIMIT_WINDOW=3600
REGISTRY_TTL=900

YOUTUBE_OAUTH_CLIENT_SECRETS=./client_secret.json
YOUTUBE_OAUTH_TOKEN_PATH=./data/oauth_readonly_token.pickle
```

Genera cada token de `API_TOKENS` (uno por cada dispositivo cliente) con:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Formato: `nombre:token,nombre2:token2`, sin espacios.

---

## OAuth de solo lectura (opcional, pero recomendado)

Sin este paso, `/api/streams` (el descubrimiento público del canal) ya
funciona. Pero `/api/streams/health` — el chequeo de salud que usan tus
scripts de streaming — necesita OAuth porque `liveStreams.list` y
`liveBroadcasts.list` lo exigen, aunque solo sea para leer.

```bash
cd /opt/yt-monitor

# Si NO tienes ya un client_secret.json en esta carpeta, cópialo:
# scp /ruta/local/client_secret.json usuario@servidor:/opt/yt-monitor/

sudo -u ytmonitor venv/bin/python3 generate_oauth_token.py
```

(Usa las rutas por defecto — `./client_secret.json` y
`./data/oauth_readonly_token.pickle` — que ya coinciden con lo que pide
el `.env.example`. Si quieres rutas distintas, pásalas con
`--client-secrets` y `--token-out`.)

El script imprime una URL y queda esperando. Si el servidor no tiene
navegador, abre un túnel SSH **desde tu PC, en otra terminal**:

```bash
ssh -L 8080:localhost:8080 usuario@ip-del-servidor
```

Deja esa terminal abierta y pega la URL en el navegador de tu PC. Tras
autorizar con la cuenta de Google dueña del canal, verás:

```
✅ Token guardado en ./data/oauth_readonly_token.pickle (permisos 600).
```

> **Atajo:** si ya tienes un `token.pickle` que otro script tuyo usa para
> gestionar el canal (crear/borrar transmisiones), puedes copiarlo
> directamente a `./data/oauth_readonly_token.pickle` en vez de generar
> uno nuevo — te ahorras el túnel SSH, a cambio de que ese token central
> tiene más permisos de los estrictamente necesarios.

Tras esto:

```bash
sudo chown ytmonitor:ytmonitor /opt/yt-monitor/client_secret.json /opt/yt-monitor/data/oauth_readonly_token.pickle
sudo chmod 600 /opt/yt-monitor/client_secret.json /opt/yt-monitor/data/oauth_readonly_token.pickle
sudo systemctl restart yt-monitor
```

---

## Arranque y gestión del servicio

```bash
sudo systemctl start yt-monitor       # arrancar
sudo systemctl status yt-monitor      # ver si está corriendo
sudo systemctl restart yt-monitor     # aplicar cambios del .env
sudo systemctl stop yt-monitor        # parar (se queda parado)
sudo journalctl -u yt-monitor -f      # ver logs en vivo (Ctrl+C para salir)
sudo journalctl -u yt-monitor -n 50   # últimas 50 líneas
```

`install.sh` ya hizo `systemctl enable`, así que arranca solo cada vez
que reinicies el servidor. `start`/`stop` controla si está corriendo
*ahora mismo* — son cosas independientes.

---

## Verificación

```bash
DOMINIO=yt-monitor.local   # o el que hayas usado al instalar

curl --cacert /etc/nginx/certs/yt-monitor.crt https://$DOMINIO/api/health
# -> {"status":"ok"}

curl -i --cacert /etc/nginx/certs/yt-monitor.crt https://$DOMINIO/api/streams
# sin token -> 401 (confirma que la seguridad funciona)

curl --cacert /etc/nginx/certs/yt-monitor.crt \
     -H "Authorization: Bearer TU_TOKEN" \
     https://$DOMINIO/api/streams
# con token -> 200 con datos del canal

curl --cacert /etc/nginx/certs/yt-monitor.crt \
     -H "Authorization: Bearer TU_TOKEN" \
     https://$DOMINIO/api/streams/health
# debe incluir "oauth_ready": true (si completaste el paso de OAuth)
```

---

## Conectar clientes (otros dispositivos)

En cada máquina que vaya a hablar con la API (tu PC incluida):

```bash
# Copia el certificado autofirmado desde el servidor
scp usuario@ip-del-servidor:/etc/nginx/certs/yt-monitor.crt .
```

Y añade el dominio a su `/etc/hosts` (o
`C:\Windows\System32\drivers\etc\hosts` en Windows, como administrador):

```
192.168.x.x   yt-monitor.local
```

Usa la IP real del servidor — resérvala en tu router (DHCP reservation)
para que no cambie.

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

## Actualizar

```bash
cd /opt/yt-monitor
git pull
sudo ./install.sh          # reinstala dependencias si requirements.txt cambió
sudo systemctl restart yt-monitor
```

`install.sh` nunca toca tu `.env` ni tus credenciales si ya existen —
solo actualiza lo que cambió en el código y las dependencias.

---

## Solución de problemas

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `curl: SSL certificate problem` | Certificado no coincide o no se copió | Revisa que el dominio usado al instalar coincide con el de `/etc/hosts` del cliente |
| `401 unauthorized` | Token mal escrito o ausente | Revisa el header `Authorization: Bearer ...` |
| `502 Bad Gateway` | El programa Python no está corriendo | `sudo systemctl status yt-monitor` + `journalctl` |
| No encuentra el dominio configurado | Falta la línea en `/etc/hosts` del cliente, o la IP cambió | Repite [Conectar clientes](#conectar-clientes-otros-dispositivos) |
| `oauth_ready: false` | Paso de OAuth no completado, o rutas mal en `.env` | Revisa `grep OAUTH /opt/yt-monitor/.env` y que los archivos existan en esa ruta relativa |
| `OAuth de solo lectura no configurado` en el log | Falta alguna línea `YOUTUBE_OAUTH_*` en el `.env` | Añádelas y reinicia |
| `429 too_many_attempts` / `rate_limit_exceeded` | Límite interno superado (no es cuota de YouTube) | Sube `RATE_LIMIT_REQUESTS` en el `.env` si es tráfico legítimo |
| Cambié el `.env` y no pasa nada | systemd solo lo lee al arrancar | `sudo systemctl restart yt-monitor` |
| `quotaExceeded` en los logs | Cuota diaria de YouTube agotada | Se resetea a medianoche hora del Pacífico (~09:00 en España) |
| `Tokens cargados para 0 cliente(s)` | Formato de `API_TOKENS` mal | Debe ser `nombre:token,nombre2:token2`, sin espacios |
| `install.sh: Permission denied` | No se ejecutó con `sudo`, o perdió el permiso de ejecución al clonar | `chmod +x install.sh && sudo ./install.sh` |
