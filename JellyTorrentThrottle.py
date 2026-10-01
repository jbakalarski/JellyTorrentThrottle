#!/usr/bin/env python3
"""
Jellyfin → qBittorrent speed controller.

Listens for Jellyfin webhook events and throttles / restores qBittorrent
download speed so that media playback is not starved of bandwidth.

Events handled:
  Play / Resume  → throttle qBittorrent (alternative speed or custom limits)
  Pause / Stop   → restore normal speed

All settings are read from a .env file — see .env.example.
"""

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

import requests as http_requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify

# ── Load .env ────────────────────────────────────────────────────────────────
load_dotenv()

WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8080"))

QB_URL: str = os.getenv("QBITTORRENT_URL", "http://localhost:8080").rstrip("/")
QB_USERNAME: str = os.getenv("QBITTORRENT_USERNAME", "admin")
QB_PASSWORD: str = os.getenv("QBITTORRENT_PASSWORD", "adminadmin")

SPEED_MODE: str = os.getenv("SPEED_MODE", "alternative").lower()  # "alternative" | "custom"
THROTTLE_DL: int = int(os.getenv("THROTTLE_DOWNLOAD_LIMIT", "1024"))  # KiB/s
THROTTLE_UL: int = int(os.getenv("THROTTLE_UPLOAD_LIMIT", "512"))     # KiB/s

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE: Optional[str] = os.getenv("LOG_FILE")

# Events that mean "someone is watching" → throttle
THROTTLE_EVENTS = {"Play", "Resume"}
# Events that mean "nobody is watching" → restore
RESTORE_EVENTS = {"Pause", "Stop"}

# ── Logging ──────────────────────────────────────────────────────────────────
log = logging.getLogger("jellyfin_qbt")
log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
_fmt = logging.Formatter("[%(asctime)s] %(levelname)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_sh)

if LOG_FILE:
    _log_dir = os.path.dirname(LOG_FILE)
    if _log_dir:
        os.makedirs(_log_dir, exist_ok=True)
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)


def _format_kib_per_s(kib: Optional[int]) -> str:
    """Format a KiB/s value for logs.

    Returns a string like "512 KiB/s" or "unlimited" for 0. None -> "unknown".
    """
    if kib is None:
        return "unknown"
    try:
        kib = int(kib)
    except Exception:
        return str(kib)
    if kib == 0:
        return "unlimited"
    return f"{kib} KiB/s"

# ── qBittorrent API client ───────────────────────────────────────────────────
_session = http_requests.Session()
_authenticated = False


def _qb_login() -> bool:
    """Authenticate with qBittorrent Web API."""
    global _authenticated
    try:
        resp = _session.post(
            f"{QB_URL}/api/v2/auth/login",
            data={"username": QB_USERNAME, "password": QB_PASSWORD},
            timeout=10,
        )
        if resp.status_code == 204 or (
            resp.status_code == 200 and resp.text.strip().lower() == "ok."
        ):
            _authenticated = True
            log.info("Authenticated with qBittorrent at %s", QB_URL)
            return True
        log.error("qBittorrent login failed: %s", resp.text.strip())
    except http_requests.RequestException as exc:
        log.error("qBittorrent connection error: %s", exc)
    _authenticated = False
    return False


def _qb_ensure_auth() -> bool:
    if _authenticated:
        return True
    return _qb_login()


def _qb_api_post(endpoint: str, data: Optional[dict] = None) -> bool:
    """POST to a qBittorrent API endpoint with automatic re-auth on 403."""
    if not _qb_ensure_auth():
        return False
    try:
        resp = _session.post(f"{QB_URL}{endpoint}", data=data, timeout=10)
        if resp.status_code == 403:
            if _qb_login():
                resp = _session.post(f"{QB_URL}{endpoint}", data=data, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.error("POST %s failed: %s", endpoint, exc)
        return False


def _qb_api_get_json(endpoint: str) -> Optional[dict]:
    """GET a qBittorrent API endpoint and return parsed JSON."""
    if not _qb_ensure_auth():
        return None
    try:
        resp = _session.get(f"{QB_URL}{endpoint}", timeout=10)
        if resp.status_code == 403:
            if _qb_login():
                resp = _session.get(f"{QB_URL}{endpoint}", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.error("GET %s failed: %s", endpoint, exc)
        return None


def _qb_set_speed_limits(dl_limit: int, ul_limit: int) -> bool:
    """Directly set global download/upload limits.

    This module uses KiB/s for internal values (from the environment).
    qBittorrent Web API expects limits in bytes/s — convert before sending.
    0 = unlimited.
    """
    dl_bytes = int(dl_limit) * 1024 if dl_limit else 0
    ul_bytes = int(ul_limit) * 1024 if ul_limit else 0

    ok_dl = _qb_api_post("/api/v2/transfer/setDownloadLimit", {"limit": dl_bytes})
    ok_ul = _qb_api_post("/api/v2/transfer/setUploadLimit", {"limit": ul_bytes})
    if ok_dl and ok_ul:
        if dl_limit == 0 and ul_limit == 0:
            log.info("Speed limits removed (unlimited)")
        else:
            log.info("Speed limits set → DL %s, UL %s",
                     _format_kib_per_s(dl_limit), _format_kib_per_s(ul_limit))
    return ok_dl and ok_ul


def _qb_fetch_alt_limits() -> tuple:
    """Read the alt-speed limits configured in qBittorrent preferences.

    Returns (alt_dl_limit, alt_up_limit) matching the unit expected by
    setDownloadLimit / setUploadLimit, or None on failure.
    """
    prefs = _qb_api_get_json("/api/v2/app/preferences")
    if prefs is None:
        return None
    alt_dl = prefs.get("alt_dl_limit", 0)
    alt_ul = prefs.get("alt_up_limit", 0)

    # qBittorrent returns bytes/s — convert to KiB/s for internal use
    alt_dl_kib = alt_dl // 1024 if alt_dl else 0
    alt_ul_kib = alt_ul // 1024 if alt_ul else 0
    log.info("Fetched qBittorrent alt-speed limits: DL %s, UL %s",
             _format_kib_per_s(alt_dl_kib), _format_kib_per_s(alt_ul_kib))
    return (alt_dl_kib, alt_ul_kib)


# ── Throttle / Restore helpers ───────────────────────────────────────────────
_throttle_dl: int = THROTTLE_DL
_throttle_ul: int = THROTTLE_UL
_limits_resolved: bool = False

# Saved limits from before throttling so we can restore them exactly.
_saved_dl: Optional[int] = None
_saved_ul: Optional[int] = None


def _qb_get_current_limits() -> Optional[tuple]:
    """Read the current global speed limits from qBittorrent.

    Returns (dl_limit, ul_limit) in KiB/s, or None on failure.
    """
    info = _qb_api_get_json("/api/v2/transfer/info")
    if info is None:
        return None
    dl = info.get("dl_rate_limit", 0)
    ul = info.get("up_rate_limit", 0)

    # Convert bytes/s -> KiB/s
    dl_kib = dl // 1024 if dl else 0
    ul_kib = ul // 1024 if ul else 0
    return (dl_kib, ul_kib)


def _resolve_throttle_limits():
    """On first call, resolve the actual byte-per-second throttle limits.

    - 'alternative' mode: reads qBittorrent's configured alt-speed limits.
    - 'custom' mode: uses THROTTLE_DL / THROTTLE_UL from .env directly.
    """
    global _throttle_dl, _throttle_ul, _limits_resolved
    if _limits_resolved:
        return
    if SPEED_MODE == "alternative":
        result = _qb_fetch_alt_limits()
        if result:
            _throttle_dl, _throttle_ul = result
            log.info("Using qBittorrent alt-speed limits: DL %s, UL %s",
                     _format_kib_per_s(_throttle_dl), _format_kib_per_s(_throttle_ul))
        else:
            log.warning("Could not fetch alt-speed limits; falling back to .env values")
    _limits_resolved = True


def throttle():
    """Save current speed limits, then apply throttle limits."""
    global _saved_dl, _saved_ul
    _resolve_throttle_limits()

    # Save current limits before overwriting (only on first throttle)
    if _saved_dl is None:
        current = _qb_get_current_limits()
        if current:
            _saved_dl, _saved_ul = current
            log.info("Saved previous limits: DL %s, UL %s",
                     _format_kib_per_s(_saved_dl), _format_kib_per_s(_saved_ul))
        else:
            _saved_dl, _saved_ul = 0, 0
            log.warning("Could not read current limits; will restore to unlimited")

    _qb_set_speed_limits(_throttle_dl, _throttle_ul)


def restore():
    """Restore speed limits to whatever they were before throttling.

    Keeps the saved values so that duplicate restore calls (e.g. Pause then
    Stop for the same session) are idempotent. The saved values are only
    overwritten when throttle() is called again.
    """
    dl = _saved_dl if _saved_dl is not None else 0
    ul = _saved_ul if _saved_ul is not None else 0
    log.info("Restoring previous limits: DL %s, UL %s",
             _format_kib_per_s(dl), _format_kib_per_s(ul))
    _qb_set_speed_limits(dl, ul)


# ── Active-playback counter ─────────────────────────────────────────────────
# Multiple users could be watching simultaneously. We only restore speed when
# the last one stops.
_active_sessions: dict = {}   # key -> readable label


def _session_key(data: dict) -> str:
    """Build a unique key for a playback session from the webhook payload."""
    session = data.get("Session", {})
    device_id = session.get("DeviceId", "")
    user_id = data.get("User", {}).get("Id", "")
    item_id = data.get("Item", {}).get("Id", "")
    return f"{user_id}:{device_id}:{item_id}"


def _session_label(data: dict) -> str:
    """Build a human-readable label for logging."""
    user = data.get("User", {}).get("Name", "?")
    item = data.get("Item", {}).get("Name", "?")
    device = data.get("Session", {}).get("DeviceName", "?")
    return f"{user} → {item} ({device})"


# ── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/", methods=["POST"])
def webhook():
    """Handle Jellyfin webhook POST."""
    try:
        data = request.get_json(force=True, silent=True)
    except Exception:
        data = None

    if not data or "Event" not in data:
        return jsonify({"error": "invalid payload"}), 400

    event = data["Event"]
    item_name = data.get("Item", {}).get("Name", "<unknown>")
    user_name = data.get("User", {}).get("Name", data.get("User", {}).get("Id", "<unknown>"))
    key = _session_key(data)

    log.info("Received event: %s | item: %s | user: %s", event, item_name, user_name)

    if event in THROTTLE_EVENTS:
        _active_sessions[key] = _session_label(data)
        log.info("Active sessions (%d): %s", len(_active_sessions),
                 ", ".join(_active_sessions.values()))
        throttle()

    elif event in RESTORE_EVENTS:
        _active_sessions.pop(key, None)
        if _active_sessions:
            log.info("Sessions remaining (%d): %s — keeping throttle",
                     len(_active_sessions), ", ".join(_active_sessions.values()))
        else:
            log.info("No active sessions — restoring qBittorrent speed")
            restore()
    else:
        log.debug("Ignoring event: %s", event)

    return jsonify({"status": "ok", "event": event}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "active_sessions": len(_active_sessions),
        "sessions": list(_active_sessions.values()),
    }), 200


# ── Entry-point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting Jellyfin → qBittorrent webhook listener")
    log.info("  Host          : %s", WEBHOOK_HOST)
    log.info("  Port          : %s", WEBHOOK_PORT)
    log.info("  qBittorrent   : %s", QB_URL)
    log.info("  Speed mode    : %s", SPEED_MODE)
    if SPEED_MODE == "custom":
        log.info("  Throttle DL   : %s", _format_kib_per_s(THROTTLE_DL))
        log.info("  Throttle UL   : %s", _format_kib_per_s(THROTTLE_UL))
    else:
        log.info("  Throttle      : will use qBittorrent alt-speed limits")
    if LOG_FILE:
        log.info("  Log file      : %s", LOG_FILE)

    app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT)
