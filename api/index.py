"""
Vercel Serverless Function (Flask): Holt YouTube-Transkripte über Supadata.

Routen:
  GET /api/transcript?url=<youtube-url>
  → JSON { "transcript": "...", "lang": "..." }
    oder { "error": "..." } mit passendem Status-Code.

Umgebungsvariablen (in Vercel setzen):
  SUPADATA_API_KEY  – API-Key von https://dash.supadata.ai
"""

import os
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

SUPADATA_BASE = "https://api.supadata.ai/v1"
# Wie lange wir auf einen async Job warten (bei langen Videos mit AI-Transkription)
JOB_POLL_INTERVAL_SEC = 2
JOB_POLL_MAX_SECONDS = 50  # Vercel Hobby-Limit liegt bei 60s


# --------------------------------------------------------------------------
# CORS für GitHub-Pages-Frontend
# --------------------------------------------------------------------------
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Cache-Control"] = "no-store"
    return response


def _api_key():
    key = os.environ.get("SUPADATA_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "SUPADATA_API_KEY ist nicht gesetzt. "
            "In Vercel unter Settings → Environment Variables eintragen."
        )
    return key


def _supadata_headers():
    return {"x-api-key": _api_key()}


# --------------------------------------------------------------------------
# Job-Polling für lange Videos (>20 Min triggern bei Supadata einen async Job)
# --------------------------------------------------------------------------
def poll_job(job_id: str) -> dict:
    """
    Pollt /transcript/{jobId} bis status='completed' oder 'failed'.
    Wirft auf Timeout oder Fehler eine Exception.
    """
    deadline = time.time() + JOB_POLL_MAX_SECONDS
    url = f"{SUPADATA_BASE}/transcript/{job_id}"
    while time.time() < deadline:
        r = requests.get(url, headers=_supadata_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status == "completed":
            return data
        if status == "failed":
            raise RuntimeError(data.get("error") or "Transkription fehlgeschlagen.")
        time.sleep(JOB_POLL_INTERVAL_SEC)
    raise TimeoutError(
        "Das Video wird noch verarbeitet. Bitte in einer Minute nochmal probieren."
    )


# --------------------------------------------------------------------------
# Hauptroute
# --------------------------------------------------------------------------
@app.route("/api/transcript", methods=["GET", "OPTIONS"])
def transcript_endpoint():
    if request.method == "OPTIONS":
        return ("", 204)

    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({
            "error": "Bitte einen YouTube-Link angeben (Parameter 'url').",
        }), 400

    try:
        # Supadata-Aufruf:
        #   text=true       → fertiger Plain-Text statt Segment-Array
        #   mode=auto       → vorhandene Untertitel nehmen, sonst per AI erzeugen
        #   KEIN lang-Param → liefert Originalsprache des Videos
        # Wir senden zusätzlich Accept-Language: * mit, damit Supadata nicht
        # versehentlich die Sprache des aufrufenden Browsers bevorzugt.
        headers = _supadata_headers()
        headers["Accept-Language"] = "*"

        r = requests.get(
            f"{SUPADATA_BASE}/transcript",
            headers=headers,
            params={"url": url, "text": "true", "mode": "auto"},
            timeout=30,
        )
    except requests.RequestException as exc:
        return jsonify({
            "error": "Verbindung zum Transkript-Dienst fehlgeschlagen.",
            "details": str(exc),
        }), 502

    # ----- Fehler vom Supadata-API in benutzerfreundliche Meldungen mappen -----
    if r.status_code == 401:
        return jsonify({
            "error": "Ungültiger oder fehlender API-Key bei Supadata. "
                     "Bitte SUPADATA_API_KEY in Vercel prüfen.",
        }), 500
    if r.status_code == 402 or r.status_code == 429:
        return jsonify({
            "error": "Das monatliche Gratis-Kontingent ist aufgebraucht "
                     "oder das Limit wurde überschritten. Bitte später nochmal versuchen.",
        }), 429
    if r.status_code == 404:
        return jsonify({
            "error": "Das Video wurde nicht gefunden, ist privat oder gelöscht.",
        }), 404
    if r.status_code == 403:
        return jsonify({
            "error": "Das Video ist gesperrt (Login, Mitgliedschaft, "
                     "Altersbeschränkung oder Geo-Block).",
        }), 403
    if r.status_code == 206:
        # Supadata: "Transcript Unavailable"
        return jsonify({
            "error": "Für dieses Video ist kein Transkript verfügbar "
                     "und es konnte auch keins erzeugt werden.",
        }), 404

    if r.status_code not in (200, 202):
        return jsonify({
            "error": "Unerwartete Antwort vom Transkript-Dienst.",
            "details": f"HTTP {r.status_code}: {r.text[:200]}",
        }), 502

    # ----- Erfolg: entweder direkt Text oder Job-ID für async Verarbeitung -----
    try:
        data = r.json()
    except ValueError:
        return jsonify({
            "error": "Antwort vom Transkript-Dienst war kein gültiges JSON.",
        }), 502

    if r.status_code == 202 or "jobId" in data:
        # Langes Video → Job pollen
        try:
            data = poll_job(data["jobId"])
        except TimeoutError as exc:
            return jsonify({"error": str(exc)}), 504
        except Exception as exc:
            return jsonify({
                "error": "Fehler bei der AI-Transkription.",
                "details": str(exc),
            }), 502

    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({
            "error": "Das Transkript ist leer. Bitte ein anderes Video versuchen.",
        }), 404

    return jsonify({
        "transcript": content,
        "lang": data.get("lang"),
        "availableLangs": data.get("availableLangs", []),
    }), 200


# Health-Check, damit man auf der Root-URL sofort sieht ob alles läuft
@app.route("/")
def root():
    has_key = bool(os.environ.get("SUPADATA_API_KEY", "").strip())
    return jsonify({
        "status": "ok",
        "endpoint": "/api/transcript?url=...",
        "api_key_configured": has_key,
    })
