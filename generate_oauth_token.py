"""
generate_oauth_token.py
------------------------
Ejecuta esto UNA SOLA VEZ en la Pi para generar el token OAuth de solo
lectura que necesita youtube_monitor.py para consultar liveStreams.list y
liveBroadcasts.list (la API Key sola no alcanza para eso).

Requiere un client_secret.json de tipo "Desktop app" — puedes REUTILIZAR
el mismo que ya usan youtube_streamer.py o yolo_youtube_pipeline.py (es el
mismo proyecto de Google Cloud), no hace falta crear uno nuevo. Lo único
distinto es el SCOPE: aquí pedimos solo 'youtube.readonly', nunca
'youtube.force-ssl' — así el token de la Pi, si se filtrara, nunca podría
crear ni borrar transmisiones, solo leer su estado.

Uso (en la Pi, headless — sin navegador local):

    python3 generate_oauth_token.py \\
        --client-secrets /opt/yt-monitor/client_secret.json \\
        --token-out /opt/yt-monitor/data/oauth_readonly_token.pickle

El script imprime una URL. Antes de abrirla, desde tu PC:

    ssh -L 8080:localhost:8080 pi@<ip-de-tu-pi>

y luego pega la URL en el navegador de tu PC. Tras autorizar, el token
queda guardado en --token-out y el script termina solo.
"""

import argparse
import os
import pickle

from google_auth_oauthlib.flow import InstalledAppFlow

# Scope MÍNIMO: de solo lectura. NUNCA uses youtube.force-ssl aquí — este
# token vive en el servidor más expuesto de tu arquitectura.
READONLY_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-secrets", required=True,
                         help="Ruta al client_secret.json (Desktop app)")
    parser.add_argument("--token-out", required=True,
                         help="Ruta donde guardar el token.pickle resultante")
    args = parser.parse_args()

    if not os.path.exists(args.client_secrets):
        raise SystemExit(f"No se encontró el client_secret.json en: {args.client_secrets}")

    if os.path.exists(args.token_out):
        resp = input(f"Ya existe un token en {args.token_out}. ¿Sobrescribir? [s/N] ")
        if resp.strip().lower() != "s":
            print("Cancelado.")
            return

    print(
        "\n🔑 Hace falta completar el consentimiento OAuth UNA sola vez.\n"
        "Si este script corre headless (sin navegador en la Pi), abre un\n"
        "túnel SSH desde tu PC ANTES de continuar:\n\n"
        "    ssh -L 8080:localhost:8080 pi@<ip-de-tu-pi>\n\n"
        "Luego pega en el navegador de tu PC la URL que se imprima abajo.\n"
    )

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, READONLY_SCOPES)
    creds = flow.run_local_server(port=8080, open_browser=False)

    os.makedirs(os.path.dirname(args.token_out) or ".", exist_ok=True)
    with open(args.token_out, "wb") as f:
        pickle.dump(creds, f)
    os.chmod(args.token_out, 0o600)

    print(f"\n✅ Token guardado en {args.token_out} (permisos 600).")
    print("   Reinicia el servicio: sudo systemctl restart yt-monitor")


if __name__ == "__main__":
    main()
