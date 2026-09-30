"""
auth.py
-------
Autenticación por bearer token para la API + rate limiting básico en memoria.

Puntos clave de seguridad implementados aquí:

1. COMPARACIÓN EN TIEMPO CONSTANTE (secrets.compare_digest)
   Un `==` normal corta la comparación en el primer byte distinto. Midiendo
   tiempos de respuesta, un atacante puede adivinar el token byte a byte
   (timing attack). compare_digest siempre tarda lo mismo.

2. TOKEN EN HEADER, NUNCA EN QUERY STRING
   Si el token va en ?token=xxx acaba en los logs de nginx, en el historial
   del navegador, en el header Referer... Los headers no se loguean por defecto.

3. TOKENS POR CLIENTE, NO UNO COMPARTIDO
   Cada Jetson tiene su propio token. Si uno se compromete, revocas ese sin
   tocar los demás, y los logs te dicen QUIÉN hizo cada llamada.

4. RATE LIMITING
   Sin esto, alguien con un token válido (o pegándole a endpoints públicos)
   puede agotarte la cuota de YouTube a propósito. Es el ataque más probable
   contra esta API en concreto.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from functools import wraps

from flask import jsonify, request

logger = logging.getLogger("auth")


class TokenRegistry:
    """
    Carga los tokens desde variable de entorno con formato:
        API_TOKENS=jetson-a:TOKEN_LARGO_1,jetson-b:TOKEN_LARGO_2

    Guarda solo el HASH del token en memoria, no el token en claro. Si alguien
    consigue un volcado de memoria del proceso, no se lleva los tokens usables.
    """

    MIN_TOKEN_LENGTH = 32

    def __init__(self, tokens_env: str):
        self._hash_to_client: dict[str, str] = {}
        self._load(tokens_env)

    def _load(self, tokens_env: str):
        if not tokens_env or not tokens_env.strip():
            raise RuntimeError(
                "API_TOKENS está vacío. Genera tokens con: "
                "python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )

        for entry in tokens_env.split(","):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            client_id, token = entry.split(":", 1)
            client_id, token = client_id.strip(), token.strip()

            if len(token) < self.MIN_TOKEN_LENGTH:
                raise RuntimeError(
                    f"El token de '{client_id}' es demasiado corto "
                    f"({len(token)} chars, mínimo {self.MIN_TOKEN_LENGTH}). "
                    "Un token corto se puede romper por fuerza bruta."
                )

            self._hash_to_client[self._hash(token)] = client_id

        if not self._hash_to_client:
            raise RuntimeError("No se cargó ningún token válido desde API_TOKENS")

        logger.info("Tokens cargados para %d cliente(s): %s",
                    len(self._hash_to_client), list(self._hash_to_client.values()))

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def verify(self, presented_token: str) -> str | None:
        """Devuelve el client_id si el token es válido, None si no lo es."""
        presented_hash = self._hash(presented_token)
        # Recorremos TODOS los tokens siempre (no cortamos al primer match)
        # y usamos compare_digest, para no filtrar información por tiempo.
        matched_client = None
        for known_hash, client_id in self._hash_to_client.items():
            if secrets.compare_digest(presented_hash, known_hash):
                matched_client = client_id
        return matched_client


class RateLimiter:
    """
    Ventana deslizante en memoria. Suficiente para un solo proceso.
    Si escalas a varios workers de gunicorn, esto NO se comparte entre
    procesos — ahí necesitarías Redis (flask-limiter con backend Redis).
    """

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        window = self._hits[key]
        while window and window[0] < now - self.window_seconds:
            window.popleft()
        if len(window) >= self.max_requests:
            return False
        window.append(now)
        return True


# --- Instancias globales, configuradas desde el entorno ---

_registry = TokenRegistry(os.environ.get("API_TOKENS", ""))

# Límite general por cliente autenticado
_general_limiter = RateLimiter(
    max_requests=int(os.environ.get("RATE_LIMIT_REQUESTS", "60")),
    window_seconds=int(os.environ.get("RATE_LIMIT_WINDOW", "60")),
)

# Límite mucho más estricto para endpoints que gastan cuota de YouTube
_expensive_limiter = RateLimiter(
    max_requests=int(os.environ.get("EXPENSIVE_LIMIT_REQUESTS", "5")),
    window_seconds=int(os.environ.get("EXPENSIVE_LIMIT_WINDOW", "3600")),
)

# Anti fuerza bruta: intentos fallidos de auth por IP
_auth_failure_limiter = RateLimiter(max_requests=10, window_seconds=300)


def _client_ip() -> str:
    """
    Detrás de nginx, request.remote_addr es siempre 127.0.0.1.
    Usamos X-Forwarded-For, que nginx debe estar configurado para setear.
    OJO: solo confía en este header si REALMENTE hay un proxy delante;
    si no, cualquiera puede falsificarlo.
    """
    if os.environ.get("BEHIND_PROXY", "false").lower() == "true":
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def require_token(expensive: bool = False):
    """
    Decorador de autenticación.

    Uso:
        @app.get("/api/streams")
        @require_token()
        def streams(): ...

        @app.post("/api/streams/refresh")
        @require_token(expensive=True)   # límite mucho más duro
        def refresh(): ...
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            ip = _client_ip()

            header = request.headers.get("Authorization", "")
            token = header[7:].strip() if header.startswith("Bearer ") else None
            client_id = _registry.verify(token) if token else None

            if not client_id:
                # El contador de fuerza bruta SOLO se consume aquí, en el
                # camino de fallo — así una petición con token válido nunca
                # gasta presupuesto de este limitador, sin importar cuántas
                # veces por minuto llegue.
                if not _auth_failure_limiter.allow(f"probe:{ip}"):
                    logger.warning("Demasiados intentos fallidos desde %s", ip)
                    return jsonify({"error": "too_many_attempts"}), 429
                logger.warning("Auth inválida desde %s", ip)
                # Mensaje genérico: no revelamos si faltaba el header, el
                # token no existe, o tiene mal formato. Menos información
                # útil para un atacante.
                return jsonify({"error": "unauthorized"}), 401

            limiter = _expensive_limiter if expensive else _general_limiter
            bucket = f"{'exp' if expensive else 'gen'}:{client_id}"
            if not limiter.allow(bucket):
                logger.warning("Rate limit excedido por %s (%s)", client_id, bucket)
                return jsonify({"error": "rate_limit_exceeded"}), 429

            request.client_id = client_id
            return fn(*args, **kwargs)

        return wrapper
    return decorator


def add_security_headers(response):
    """Cabeceras de seguridad. Registrar con @app.after_request."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    # HSTS solo tiene sentido si sirves por HTTPS
    if os.environ.get("BEHIND_PROXY", "false").lower() == "true":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response
