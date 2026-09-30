"""
youtube_monitor.py
-------------------
Escanea un canal de YouTube periódicamente y mantiene en memoria
el estado de todos los streams (en vivo, próximos, finalizados recientes).

Usa DOS credenciales distintas, para dos tipos de datos distintos:

  1. API Key (self._youtube) — para datos PÚBLICOS del canal:
     - search/playlistItems/videos.list -> descubrimiento del canal.
     Funciona con una simple API Key porque es información pública
     (o "no listada pero conocido el ID", que es casi lo mismo a
     efectos de esta API).

  2. OAuth de solo lectura (self._youtube_oauth) — para datos PRIVADOS
     de "tu propio canal":
     - liveStreams.list / liveBroadcasts.list -> streamStatus,
       healthStatus, lifeCycleStatus.
     YouTube EXIGE OAuth para esto (scope youtube.readonly basta), una
     API Key no sirve por más que sea de solo lectura — son recursos de
     gestión del canal, no datos públicos. Ver:
     https://developers.google.com/youtube/v3/live/docs/liveBroadcasts/list

Diseño de cuota:
  - search.list(eventType=live/upcoming)  -> descubre video_ids  (100 units c/u) — NO SE USA, ver abajo
  - playlistItems.list sobre 'uploads'     -> 1 unidad
  - videos.list(part=liveStreamingDetails,statistics,snippet)
                                           -> detalle + viewers  (1 unit por bloque de 50 ids)
  - liveStreams.list / liveBroadcasts.list -> 1 unidad cada una (hasta 50 ids por llamada)

Esto minimiza cuota: el "descubrimiento" es barato (playlist, no search),
y los chequeos de salud de todos los streams registrados van agrupados en
una sola llamada cada uno, sin importar cuántos Jetsons los usen.
"""

from __future__ import annotations

import logging
import os
import pickle
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger("yt_monitor")

# Scope mínimo posible para leer status de streams/broadcasts: NO permite
# crear, borrar ni modificar nada. Aunque el token se filtrara, el daño
# posible es leer el estado de tus propias transmisiones, nada más.
OAUTH_READONLY_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


@dataclass
class StreamInfo:
    video_id: str
    title: str
    status: str  # "live" | "upcoming" | "ended"
    scheduled_start_time: Optional[str] = None
    actual_start_time: Optional[str] = None
    actual_end_time: Optional[str] = None
    concurrent_viewers: Optional[int] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    thumbnail_url: Optional[str] = None
    description: Optional[str] = None
    channel_id: Optional[str] = None
    channel_title: Optional[str] = None
    url: str = field(init=False, default="")

    def __post_init__(self):
        self.url = f"https://www.youtube.com/watch?v={self.video_id}"

    def to_dict(self):
        return asdict(self)


class YouTubeMonitor:
    def __init__(self, api_key: str, channel_id: str, poll_interval: int = 120,
                 oauth_client_secrets_path: Optional[str] = None,
                 oauth_token_path: Optional[str] = None):
        self.api_key = api_key
        self.channel_id = channel_id
        self.poll_interval = poll_interval

        self._youtube = build("youtube", "v3", developerKey=self.api_key)

        # OAuth de solo lectura, SEPARADO de la API Key. liveStreams.list y
        # liveBroadcasts.list exigen OAuth (scope youtube.readonly como
        # mínimo) — una API Key devuelve 403 aunque solo sea para leer.
        # Si no se configura, los chequeos de salud agrupados quedan
        # deshabilitados pero el descubrimiento público del canal sigue
        # funcionando con normalidad.
        self._youtube_oauth = None
        self._oauth_configured = bool(oauth_client_secrets_path and oauth_token_path)
        if self._oauth_configured:
            self._youtube_oauth = self._build_oauth_client(
                oauth_client_secrets_path, oauth_token_path
            )
        else:
            logger.warning(
                "OAuth de solo lectura no configurado — los chequeos de salud de "
                "streams/broadcasts (liveStreams.list/liveBroadcasts.list) quedan "
                "deshabilitados. Genera el token con generate_oauth_token.py."
            )

        self._lock = threading.Lock()
        self._state: dict[str, StreamInfo] = {}
        self._last_updated: Optional[str] = None
        self._last_error: Optional[str] = None
        self._quota_used_estimate = 0
        self._uploads_playlist_id: Optional[str] = None  # se resuelve una sola vez
        self._watched_stream_ids: set[str] = set()
        self._watched_broadcast_ids: set[str] = set()
        self._stream_health: dict[str, dict] = {}
        self._broadcast_health: dict[str, dict] = {}

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def _build_oauth_client(client_secrets_path: str, token_path: str):
        """Carga (o refresca) el token OAuth de solo lectura desde disco.
        NUNCA dispara el flujo de consentimiento interactivo aquí — eso
        corre una sola vez, por separado, con generate_oauth_token.py.
        Si el token no existe o no se puede refrescar, deja los chequeos
        de salud deshabilitados en vez de tumbar el proceso entero."""
        if not os.path.exists(token_path):
            logger.error(
                "No existe el token OAuth en '%s'. Ejecuta primero: "
                "python3 generate_oauth_token.py", token_path
            )
            return None
        try:
            with open(token_path, "rb") as f:
                creds = pickle.load(f)
            if not creds.valid:
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(token_path, "wb") as f:
                        pickle.dump(creds, f)
                else:
                    raise RuntimeError("Token inválido y sin refresh_token utilizable")
            return build("youtube", "v3", credentials=creds)
        except Exception as e:
            logger.error("No se pudo cargar el token OAuth de solo lectura: %s", e)
            return None

    # ---------- API pública para Flask ----------

    def start(self):
        """Arranca el hilo de polling en background."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Monitor iniciado (intervalo=%ss)", self.poll_interval)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_snapshot(self) -> dict:
        """Devuelve el estado actual (thread-safe) para servir por la API."""
        with self._lock:
            return {
                "channel_id": self.channel_id,
                "last_updated": self._last_updated,
                "last_error": self._last_error,
                "quota_used_estimate": self._quota_used_estimate,
                "poll_interval_seconds": self.poll_interval,
                "streams": [s.to_dict() for s in self._state.values()],
            }

    def get_by_status(self, status: str) -> list[dict]:
        with self._lock:
            return [s.to_dict() for s in self._state.values() if s.status == status]

    def force_refresh(self):
        """Fuerza un escaneo inmediato (úsalo con cuidado, gasta cuota)."""
        self._scan_once()

    def register_stream_ids(self, stream_ids: list[str]):
        """La API central le dice al monitor qué stream_ids debe vigilar."""
        with self._lock:
            self._watched_stream_ids = set(stream_ids)

    def register_broadcast_ids(self, broadcast_ids: list[str]):
        """Igual que register_stream_ids, pero para el lifeCycleStatus del
        broadcast — necesario para detectar cuándo el evento entero murió
        (no solo el punto de ingesta RTMP)."""
        with self._lock:
            self._watched_broadcast_ids = set(broadcast_ids)

    def get_stream_health(self) -> dict:
        """Estado cacheado de los streams vigilados (no gasta cuota al leerlo)."""
        with self._lock:
            return dict(self._stream_health)

    def get_broadcast_health(self) -> dict:
        """Estado cacheado (lifeCycleStatus) de los broadcasts vigilados."""
        with self._lock:
            return dict(self._broadcast_health)

    def oauth_ready(self) -> bool:
        return self._youtube_oauth is not None

    def _check_watched_streams(self):
        """
        liveStreams.list acepta hasta 50 IDs por llamada y cuesta 1 unidad
        SIN IMPORTAR cuántos IDs incluyas. Aquí está el ahorro real frente
        a que cada Jetson pregunte por su cuenta. Requiere OAuth (ver
        _build_oauth_client) — con solo la API Key, YouTube devuelve 403.
        """
        if not self._youtube_oauth:
            return
        with self._lock:
            ids = list(self._watched_stream_ids)
        if not ids:
            return

        health = {}
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            try:
                resp = self._youtube_oauth.liveStreams().list(
                    part="status",
                    id=",".join(batch),
                ).execute()
                self._quota_used_estimate += 1
            except HttpError as e:
                logger.error("liveStreams.list falló: %s", e)
                continue

            for item in resp.get("items", []):
                health[item["id"]] = {
                    "stream_status": item.get("status", {}).get("streamStatus"),
                    "health_status": (item.get("status", {})
                                      .get("healthStatus", {}).get("status")),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

        # IDs que pedimos pero YouTube no devolvió = ya no existen
        for sid in ids:
            if sid not in health:
                health[sid] = {"stream_status": "notFound",
                               "health_status": None,
                               "checked_at": datetime.now(timezone.utc).isoformat()}

        with self._lock:
            self._stream_health = health

    def _check_watched_broadcasts(self):
        """
        Igual que _check_watched_streams pero para liveBroadcasts.list,
        que da el lifeCycleStatus (detecta 'complete'/'revoked' — el
        broadcast entero murió y hace falta uno nuevo, reconectar el
        mismo punto RTMP no sirve). También requiere OAuth.
        """
        if not self._youtube_oauth:
            return
        with self._lock:
            ids = list(self._watched_broadcast_ids)
        if not ids:
            return

        health = {}
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            try:
                resp = self._youtube_oauth.liveBroadcasts().list(
                    part="status",
                    id=",".join(batch),
                ).execute()
                self._quota_used_estimate += 1
            except HttpError as e:
                logger.error("liveBroadcasts.list falló: %s", e)
                continue

            for item in resp.get("items", []):
                health[item["id"]] = {
                    "lifecycle_status": item.get("status", {}).get("lifeCycleStatus"),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

        for bid in ids:
            if bid not in health:
                health[bid] = {"lifecycle_status": "notFound",
                               "checked_at": datetime.now(timezone.utc).isoformat()}

        with self._lock:
            self._broadcast_health = health

    # ---------- Lógica interna ----------

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self._scan_once()
            except Exception as exc:  # nunca tumbar el hilo
                logger.exception("Error en el ciclo de escaneo")
                with self._lock:
                    self._last_error = str(exc)
            self._stop_event.wait(self.poll_interval)

    def _scan_once(self):
        logger.info("Escaneando canal %s...", self.channel_id)

        video_ids = self._discover_recent_video_ids()

        streams: dict[str, StreamInfo] = {}
        if video_ids:
            streams = self._fetch_video_details(video_ids)
            # Solo nos quedamos con lo que de verdad es live/upcoming (descartamos
            # los subidos normales que la playlist también trae).
            streams = {vid: info for vid, info in streams.items()
                       if info.status in ("live", "upcoming")}

        with self._lock:
            self._state = streams
            self._last_updated = datetime.now(timezone.utc).isoformat()
            self._last_error = None

        self._check_watched_streams()
        self._check_watched_broadcasts()
        logger.info("Escaneo completo: %d streams encontrados", len(streams))

    def _get_uploads_playlist_id(self) -> Optional[str]:
        """channels.list -> 1 unidad. Se cachea porque este ID nunca cambia."""
        if self._uploads_playlist_id:
            return self._uploads_playlist_id
        try:
            resp = self._youtube.channels().list(
                part="contentDetails",
                id=self.channel_id,
            ).execute()
            self._quota_used_estimate += 1
            items = resp.get("items", [])
            if not items:
                raise ValueError(f"Canal {self.channel_id} no encontrado")
            self._uploads_playlist_id = (
                items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
            )
            return self._uploads_playlist_id
        except (HttpError, ValueError) as e:
            logger.error("channels.list falló: %s", e)
            with self._lock:
                self._last_error = f"channels.list: {e}"
            return None

    def _discover_recent_video_ids(self, max_results: int = 15) -> set[str]:
        """
        playlistItems.list sobre la playlist de 'uploads' -> 1 unidad.
        Los streams (en vivo o programados) aparecen aquí apenas se crean,
        igual que un video subido normal. Mucho más barato que search.list,
        pero SOLO ve contenido público — un stream no listado/privado no
        aparecerá nunca por esta vía (ni por ninguna vía no autenticada).
        """
        playlist_id = self._get_uploads_playlist_id()
        if not playlist_id:
            return set()

        try:
            resp = self._youtube.playlistItems().list(
                part="contentDetails",
                playlistId=playlist_id,
                maxResults=max_results,
            ).execute()
            self._quota_used_estimate += 1
            return {item["contentDetails"]["videoId"] for item in resp.get("items", [])}
        except HttpError as e:
            logger.error("playlistItems.list falló: %s", e)
            with self._lock:
                self._last_error = f"playlistItems.list: {e}"
            return set()

    def _fetch_video_details(self, video_ids: set[str]) -> dict[str, StreamInfo]:
        """videos.list -> 1 unidad de cuota cada request (hasta 50 ids por request)."""
        result: dict[str, StreamInfo] = {}
        ids_list = list(video_ids)

        for i in range(0, len(ids_list), 50):
            batch = ids_list[i:i + 50]
            try:
                resp = self._youtube.videos().list(
                    part="snippet,liveStreamingDetails,statistics",
                    id=",".join(batch),
                ).execute()
                self._quota_used_estimate += 1
            except HttpError as e:
                logger.error("videos.list falló: %s", e)
                continue

            for item in resp.get("items", []):
                info = self._parse_video_item(item)
                if info:
                    result[info.video_id] = info

        return result

    @staticmethod
    def _parse_video_item(item: dict) -> Optional[StreamInfo]:
        snippet = item.get("snippet", {})
        live_details = item.get("liveStreamingDetails", {})
        stats = item.get("statistics", {})

        broadcast_status = snippet.get("liveBroadcastContent", "none")
        if broadcast_status == "live":
            status = "live"
        elif broadcast_status == "upcoming":
            status = "upcoming"
        else:
            status = "ended"

        thumbnails = snippet.get("thumbnails", {})
        thumb = (thumbnails.get("high") or thumbnails.get("default") or {}).get("url")

        return StreamInfo(
            video_id=item["id"],
            title=snippet.get("title", ""),
            status=status,
            scheduled_start_time=live_details.get("scheduledStartTime"),
            actual_start_time=live_details.get("actualStartTime"),
            actual_end_time=live_details.get("actualEndTime"),
            concurrent_viewers=(
                int(live_details["concurrentViewers"])
                if "concurrentViewers" in live_details else None
            ),
            view_count=int(stats.get("viewCount", 0)) if stats.get("viewCount") else None,
            like_count=int(stats.get("likeCount", 0)) if stats.get("likeCount") else None,
            thumbnail_url=thumb,
            description=snippet.get("description"),
            channel_id=snippet.get("channelId"),
            channel_title=snippet.get("channelTitle"),
        )
