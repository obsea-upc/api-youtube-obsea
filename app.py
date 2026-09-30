"""
app.py
------
API Flask protegida con bearer token, pensada para desplegarse en un servidor
público. Centraliza el registro de streams activos y hace el chequeo de salud
agrupado contra la YouTube Data API.

NUNCA ejecutes esto con app.run() en producción. Usa gunicorn detrás de nginx
con HTTPS (ver README).
"""

from __future__ import annotations

import logging
import os
import time

from flask import Flask, jsonify, request

from auth import add_security_headers, require_token
from youtube_monitor import YouTubeMonitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("app")

API_KEY = os.environ["YOUTUBE_API_KEY"]
CHANNEL_ID = os.environ["YOUTUBE_CHANNEL_ID"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))

# OAuth de solo lectura (opcional pero necesario para /api/streams/health).
# Generar con: python3 generate_oauth_token.py
OAUTH_CLIENT_SECRETS = os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRETS", "").strip() or None
OAUTH_TOKEN_PATH = os.environ.get("YOUTUBE_OAUTH_TOKEN_PATH", "").strip() or None

# TTL del registro: si un Jetson no manda heartbeat en este tiempo,
# se considera muerto y deja de consumir cuota en los chequeos agrupados.
REGISTRY_TTL = int(os.environ.get("REGISTRY_TTL", "900"))

app = Flask(__name__)

# Rechaza cuerpos gigantes (protección básica contra DoS por payload)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

app.after_request(add_security_headers)

monitor = YouTubeMonitor(
    api_key=API_KEY, channel_id=CHANNEL_ID, poll_interval=POLL_INTERVAL,
    oauth_client_secrets_path=OAUTH_CLIENT_SECRETS,
    oauth_token_path=OAUTH_TOKEN_PATH,
)
monitor.start()


# ---------------------------------------------------------------------------
# Registro centralizado de streams activos
# ---------------------------------------------------------------------------

_registry: dict[str, dict] = {}


def _purge_stale():
    now = time.time()
    dead = [k for k, v in _registry.items() if now - v["last_seen"] > REGISTRY_TTL]
    for k in dead:
        logger.info("Purgando registro caducado: %s", k)
        _registry.pop(k, None)
    if dead:
        _sync_watch_lists()


def _sync_watch_lists():
    """Tras cualquier cambio en el registro, le decimos al monitor qué
    stream_ids/broadcast_ids vigilar en el próximo chequeo agrupado."""
    stream_ids = [v["stream_id"] for v in _registry.values() if v["stream_id"]]
    broadcast_ids = [v["broadcast_id"] for v in _registry.values() if v["broadcast_id"]]
    monitor.register_stream_ids(stream_ids)
    monitor.register_broadcast_ids(broadcast_ids)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    """Único endpoint SIN auth: solo confirma que el proceso responde.
    No revela ninguna información sobre streams, cuota ni configuración."""
    return jsonify({"status": "ok"})


@app.post("/api/register")
@require_token()
def register():
    """
    Un Jetson registra (o refresca) su stream activo.
    Body: {"node_id": "jetson-a", "broadcast_id": "...", "stream_id": "..."}
    """
    data = request.get_json(silent=True) or {}

    node_id = str(data.get("node_id", "")).strip()
    broadcast_id = str(data.get("broadcast_id", "")).strip()
    stream_id = str(data.get("stream_id", "")).strip()

    # Validación estricta: nunca confíes en lo que llega por la red
    if not node_id or not stream_id:
        return jsonify({"error": "node_id y stream_id son obligatorios"}), 400
    if len(node_id) > 64 or len(broadcast_id) > 64 or len(stream_id) > 64:
        return jsonify({"error": "campos demasiado largos"}), 400
    if not all(c.isalnum() or c in "-_" for c in stream_id):
        return jsonify({"error": "stream_id con caracteres inválidos"}), 400

    _registry[node_id] = {
        "node_id": node_id,
        "broadcast_id": broadcast_id,
        "stream_id": stream_id,
        "registered_by": request.client_id,
        "last_seen": time.time(),
    }
    logger.info("Registro de %s (cliente=%s)", node_id, request.client_id)
    _purge_stale()
    _sync_watch_lists()

    return jsonify({
        "ok": True, "registered": node_id, "ttl_seconds": REGISTRY_TTL,
        "oauth_ready": monitor.oauth_ready(),
    })


@app.post("/api/unregister")
@require_token()
def unregister():
    data = request.get_json(silent=True) or {}
    node_id = str(data.get("node_id", "")).strip()
    removed = _registry.pop(node_id, None)
    _sync_watch_lists()
    return jsonify({"ok": True, "removed": bool(removed)})


@app.get("/api/streams/health")
@require_token()
def streams_health():
    """
    Lo que consultan los Jetsons en vez de llamar a YouTube directamente.
    El chequeo agrupado real lo hace el monitor en background: aquí solo
    se sirve el resultado cacheado, así que este endpoint no gasta cuota.
    """
    _purge_stale()
    return jsonify({
        "nodes": list(_registry.values()),
        "stream_status": monitor.get_stream_health(),
        "broadcast_status": monitor.get_broadcast_health(),
        "oauth_ready": monitor.oauth_ready(),
        "last_updated": monitor.get_snapshot()["last_updated"],
    })


@app.get("/api/streams")
@require_token()
def all_streams():
    return jsonify(monitor.get_snapshot())


@app.get("/api/streams/live")
@require_token()
def live_streams():
    return jsonify({"streams": monitor.get_by_status("live")})


@app.get("/api/streams/upcoming")
@require_token()
def upcoming_streams():
    return jsonify({"streams": monitor.get_by_status("upcoming")})


@app.post("/api/streams/refresh")
@require_token(expensive=True)
def force_refresh():
    """Gasta cuota de YouTube -> rate limit mucho más estricto."""
    logger.info("Refresh forzado por %s", request.client_id)
    monitor.force_refresh()
    return jsonify(monitor.get_snapshot())


@app.errorhandler(404)
def not_found(_):
    return jsonify({"error": "not_found"}), 404


@app.errorhandler(500)
def server_error(e):
    # Nunca devuelvas el traceback al cliente: filtra rutas, versiones y lógica interna
    logger.exception("Error interno")
    return jsonify({"error": "internal_error"}), 500


if __name__ == "__main__":
    # SOLO para desarrollo local. En producción: gunicorn detrás de nginx.
    logger.warning("Servidor de desarrollo - NO usar en producción")
    app.run(host="127.0.0.1", port=5000, debug=False)
