#!/usr/bin/env python3
"""
Addon Home Assistant: comandar o portão MConnect a partir do HA.
Usa o link de partilha (app MConnect) uma vez — sem login, sem email, sem códigos.
O addon troca home_id+access_code por token e renova quando expira; o utilizador só configura o link.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None
from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DATA_DIR = Path("/data")
STATE_PATH = DATA_DIR / "motorline_share_state.json"
if not DATA_DIR.exists():
    DATA_DIR = Path(__file__).parent
    STATE_PATH = DATA_DIR / "motorline_share_state.json"

API_BASE_URL = "https://rest.mconnect.pt"
REFRESH_BEFORE_EXPIRY_SECONDS = 300
GUEST_DEVICE_UUID = "motorline-mconnect-share-addon-1"
COOKIES_PATH = DATA_DIR / "motorline_share_cookies.json"
ADDON_OPTIONS_PATH = DATA_DIR / "options.json"

_token = None
_token_expires_at = 0.0
_lock = threading.Lock()
_session: requests.Session | None = None
_session_lock = threading.Lock()
_last_guest_exchange_error_at = 0.0
_last_guest_exchange_max_devices_at = 0.0
GUEST_EXCHANGE_COOLDOWN_SECONDS = 300
GUEST_MAX_DEVICES_COOLDOWN_SECONDS = 3600

app = Flask(__name__)


def get_http_session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            _session = requests.Session()
            _session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Accept-Language": "pt,en-US;q=0.9,en;q=0.8",
                "Origin": "https://mconnect.pt",
                "Content-Type": "application/json",
            })
            _load_session_cookies()
        return _session


def _load_session_cookies():
    if not COOKIES_PATH.exists() or _session is None:
        return
    try:
        with open(COOKIES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for c in data if isinstance(data, list) else []:
            if isinstance(c, dict) and c.get("name") and c.get("value") is not None:
                _session.cookies.set(
                    c["name"], c["value"],
                    domain=c.get("domain") or "",
                    path=c.get("path") or "/",
                    expires=c.get("expires"),
                )
    except Exception as e:
        logger.debug("load_session_cookies: %s", e)


def _save_session_cookies():
    if _session is None:
        return
    try:
        out = [{"name": c.name, "value": c.value, "domain": getattr(c, "domain", "") or "", "path": getattr(c, "path", "") or "/", "expires": getattr(c, "expires", None)} for c in _session.cookies]
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(COOKIES_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=0)
    except Exception as e:
        logger.debug("save_session_cookies: %s", e)


def _parse_api_token_expiry(header_value: str) -> float:
    if not header_value or not isinstance(header_value, str):
        return 0.0
    val = header_value.strip()
    if not val:
        return 0.0
    try:
        return float(int(val))
    except ValueError:
        pass
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    return 0.0


def _update_token_from_response(r: requests.Response) -> None:
    global _token, _token_expires_at
    if not r or getattr(r, "headers", None) is None:
        return
    headers = r.headers
    auth = headers.get("Authorization") or headers.get("authorization")
    new_token = None
    if auth and isinstance(auth, str) and auth.strip().lower().startswith("bearer "):
        new_token = auth[7:].strip()
    expiry = _parse_api_token_expiry(headers.get("API-Token-Expiry") or headers.get("api-token-expiry") or "")
    if new_token:
        with _lock:
            _token = new_token
            if expiry > 0:
                _token_expires_at = expiry
            save_state({"token": _token, "token_expires_at": _token_expires_at})
    elif expiry > 0 and _token:
        with _lock:
            _token_expires_at = expiry
            save_state({"token_expires_at": _token_expires_at})


def load_addon_options() -> dict:
    if not ADDON_OPTIONS_PATH.exists():
        return {}
    try:
        with open(ADDON_OPTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.debug("load_addon_options: %s", e)
        return {}


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("load_state falhou: %s", e)
        return {}


def save_state(updates: dict):
    state = load_state()
    state.update(updates)
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning("save_state falhou: %s", e)


def load_options() -> dict:
    state = load_state()
    return {
        "token": (state.get("token") or "").strip() or None,
        "token_expires_at": state.get("token_expires_at") or 0,
        "guest_home_id": (state.get("guest_home_id") or "").strip() or None,
        "guest_access_code": (state.get("guest_access_code") or "").strip() or None,
        "device_id": (state.get("device_id") or "").strip(),
    }


def parse_shareable_link(link: str) -> tuple[str | None, str | None]:
    link = (link or "").strip()
    if not link:
        return None, None
    try:
        parsed = urlparse(link)
        qs = parse_qs(parsed.query)
        home_id = (qs.get("home_id") or [None])[0]
        access_code = (qs.get("access_code") or [None])[0]
        if isinstance(home_id, str) and isinstance(access_code, str):
            home_id, access_code = home_id.strip(), access_code.strip()
            if home_id and access_code:
                return home_id, access_code
    except Exception as e:
        logger.debug("parse_shareable_link: %s", e)
    return None, None


def guest_exchange_link_for_token(home_id: str, access_code: str) -> tuple[str | None, int]:
    global _last_guest_exchange_error_at, _last_guest_exchange_max_devices_at
    base = API_BASE_URL.rstrip("/")
    headers = {"Content-Type": "application/json", "Origin": "https://mconnect.pt", "timezone": "Europe/Lisbon"}
    session = get_http_session()
    try:
        r1 = session.post(
            f"{base}/auth/token",
            json={"grant_type": "authorization", "home_id": home_id, "access_code": access_code, "platform": "Linux", "model": "addon", "uuid": GUEST_DEVICE_UUID},
            headers=headers,
            timeout=15,
        )
        if r1.cookies:
            _save_session_cookies()
        if r1.status_code != 200 or not r1.text:
            body = (r1.text or "")[:200]
            logger.warning("Guest POST /auth/token: status=%s %s", r1.status_code, body)
            now = time.time()
            _last_guest_exchange_error_at = now
            if r1.status_code == 403 and "MaxTrustedDevicesError" in body:
                _last_guest_exchange_max_devices_at = now
            return None, 0
        data1 = r1.json()
        first_token = data1.get("access_token") or data1.get("token") or data1.get("accessToken")
        if not first_token:
            logger.warning("Guest /auth/token sem access_token: %s", data1)
            return None, 0
        r2 = session.post(
            f"{base}/homes/auth/token",
            json={"grant_type": "authorization", "code": first_token, "home_id": home_id},
            headers=headers,
            timeout=15,
        )
        if r2.cookies:
            _save_session_cookies()
        if r2.status_code != 200 or not r2.text:
            logger.warning("Guest POST /homes/auth/token: status=%s %s", r2.status_code, (r2.text or "")[:200])
            _last_guest_exchange_error_at = time.time()
            return None, 0
        data2 = r2.json()
        token = data2.get("access_token") or data2.get("token") or data2.get("accessToken")
        if not token:
            logger.warning("Guest /homes/auth/token sem access_token: %s", data2)
            return None, 0
        expires_in = int(data2.get("expires_in", data2.get("expiresIn", 3600)))
        logger.info("Token de partilha obtido (home_id=%s, expira em %s s)", home_id[:12], expires_in)
        return token, expires_in
    except Exception as e:
        logger.warning("guest_exchange_link_for_token: %s", e)
        return None, 0


def ensure_token() -> tuple[str | None, str]:
    global _token, _token_expires_at, _last_guest_exchange_error_at, _last_guest_exchange_max_devices_at
    opts = load_options()
    now = time.time()
    persisted = (opts.get("token") or "").strip()
    persisted_exp = float(opts.get("token_expires_at") or 0)
    if not _token and persisted:
        _token = persisted
        _token_expires_at = persisted_exp or 0.0
    if _token:
        if _token_expires_at and (now + REFRESH_BEFORE_EXPIRY_SECONDS) >= _token_expires_at and opts.get("guest_home_id") and opts.get("guest_access_code"):
            if _last_guest_exchange_max_devices_at and (now - _last_guest_exchange_max_devices_at) < GUEST_MAX_DEVICES_COOLDOWN_SECONDS:
                return _token, "O serviço recusou novas sessões (MaxTrustedDevicesError). Termine sessões noutros dispositivos ou gere novo link."
            if _last_guest_exchange_error_at and (now - _last_guest_exchange_error_at) < GUEST_EXCHANGE_COOLDOWN_SECONDS:
                return _token, "A renovar token em backoff devido a erros recentes."
            token, exp = guest_exchange_link_for_token(opts["guest_home_id"], opts["guest_access_code"])
            if token:
                with _lock:
                    _token = token
                    _token_expires_at = time.time() + exp
                    save_state({"token": _token, "token_expires_at": _token_expires_at})
                logger.info("Token de partilha renovado proativamente.")
        return _token, ""
    if opts.get("guest_home_id") and opts.get("guest_access_code"):
        if _last_guest_exchange_max_devices_at and (now - _last_guest_exchange_max_devices_at) < GUEST_MAX_DEVICES_COOLDOWN_SECONDS:
            return None, "Foi atingido o número máximo de sessões neste serviço. Termine a sessão noutros dispositivos/app MConnect ou gere um novo link de partilha."
        if _last_guest_exchange_error_at and (now - _last_guest_exchange_error_at) < GUEST_EXCHANGE_COOLDOWN_SECONDS:
            return None, "A aguardar antes de tentar renovar o token novamente devido a erros recentes."
        token, exp = guest_exchange_link_for_token(opts["guest_home_id"], opts["guest_access_code"])
        if token:
            with _lock:
                _token = token
                _token_expires_at = now + exp
                save_state({"token": _token, "token_expires_at": _token_expires_at})
            return _token, ""
    return None, "Cole o link de partilha no painel, clique em Ativar ou verifique o link / sessões ativas na app."


def _get_rooms(token: str) -> list[dict]:
    base = API_BASE_URL.rstrip("/")
    headers = {"Content-Type": "application/json", "timezone": "Europe/Lisbon", "Authorization": f"Bearer {token}"}
    session = get_http_session()
    try:
        r = session.get(f"{base}/rooms", headers=headers, timeout=15)
        if r.cookies:
            _save_session_cookies()
        if r.status_code == 200:
            _update_token_from_response(r)
            if r.text:
                raw = r.json()
                return raw if isinstance(raw, list) else []
        if r.status_code != 200:
            logger.warning("GET /rooms devolveu %s: %s", r.status_code, r.text[:200] if r.text else "")
    except Exception as e:
        logger.debug("_get_rooms: %s", e)
    return []


GATE_STATE_MAP = {0: "fechado", 2: "aberto", 6: "fechando", 8: "abrindo"}


def gate_value_to_state(raw_value: int | float) -> str:
    v = int(raw_value) if raw_value is not None else 0
    return GATE_STATE_MAP.get(v, "desconhecido")


def get_gate_state(device_id: str, token: str) -> dict | None:
    for room in _get_rooms(token):
        for d in room.get("devices", []):
            if not isinstance(d, dict):
                continue
            did = d.get("_id", d.get("id", d.get("device_id", "")))
            if did != device_id:
                continue
            for v in d.get("values", []):
                if isinstance(v, dict) and v.get("value_id") == "gate_state":
                    raw = v.get("value", 0)
                    return {"value": raw, "state": gate_value_to_state(raw), "unit": v.get("unit", "%")}
    return None


def get_devices(token: str) -> list[dict]:
    rooms = _get_rooms(token)
    out = []
    for room in rooms:
        for d in room.get("devices", []):
            if isinstance(d, dict):
                out.append({"id": d.get("_id", d.get("id", d.get("device_id", ""))), "name": d.get("name", d.get("label", ""))})
    return out


def get_first_gate_device_id(token: str) -> str | None:
    """Obtém o device_id do primeiro dispositivo com gate_state (portão) em GET /rooms."""
    for room in _get_rooms(token):
        for d in room.get("devices", []):
            if not isinstance(d, dict):
                continue
            for v in d.get("values", []):
                if isinstance(v, dict) and v.get("value_id") == "gate_state":
                    return d.get("_id") or d.get("id") or d.get("device_id") or None
    return None


def _post_device_value(device_id: str, num: int, token: str, body: dict | None = None) -> tuple[bool, int, str]:
    base = API_BASE_URL.rstrip("/")
    url = f"{base}/devices/value/{device_id}"
    payload = body or {"value_id": "gate_state", "value": num}
    headers = {"Content-Type": "application/json", "Origin": "https://mconnect.pt", "timezone": "Europe/Lisbon", "Accept": "*/*", "Authorization": f"Bearer {token}"}
    session = get_http_session()
    r = session.post(url, json=payload, headers=headers, timeout=15)
    if r.cookies:
        _save_session_cookies()
    if r.status_code in (200, 204):
        _update_token_from_response(r)
        return True, r.status_code, ""
    return False, r.status_code, r.text[:300] if r.text else ""


def set_device_value(device_id: str, value: str | int | float) -> tuple[bool, str]:
    num = 2 if value in (1, "1", "open") else int(value) if isinstance(value, (int, float)) else 2
    token, error_msg = ensure_token()
    if not token:
        return False, error_msg or "Falha ao obter token"
    ok, status, err = _post_device_value(device_id, num, token)
    if ok:
        # Regista momento do último comando para permitir polling MQTT mais frequente logo após a ordem
        try:
            save_state({"last_command_at": time.time()})
        except Exception:
            pass
        return True, ""
    if status == 401:
        new_token, msg = ensure_token()
        if new_token and new_token != token:
            ok2, _, _ = _post_device_value(device_id, num, new_token)
            if ok2:
                return True, ""
        if msg:
            error_msg = msg
    return False, (f"HTTP {status}: {err}" if status else (error_msg or ""))


def _format_token_expiry(expires_at: float) -> str | None:
    if not expires_at or expires_at <= 0:
        return None
    try:
        return datetime.fromtimestamp(expires_at).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return None


# ----- MQTT -----
MQTT_TOPIC_STATE = "motorline/share/portao/state"
MQTT_TOPIC_COMMAND = "motorline/share/portao/command"
MQTT_DISCOVERY_PREFIX = "homeassistant"
MQTT_STATE_IDLE_INTERVAL = 1800   # intervalo quando está tudo parado
MQTT_STATE_ACTIVE_INTERVAL = 5    # intervalo após comando recente (até estabilizar)


def _mqtt_publish_state(client):
    opts = load_options()
    device_id = (opts.get("device_id") or "").strip()
    if not device_id:
        return
    token, _ = ensure_token()
    if not token:
        return
    gate = get_gate_state(device_id, token)
    if gate is None:
        return
    client.publish(MQTT_TOPIC_STATE, json.dumps({"state": gate.get("state", "desconhecido"), "value": gate.get("value", 0)}), retain=True)


def _mqtt_thread():
    if not mqtt:
        return
    addon = load_addon_options()
    if not addon.get("mqtt_enabled"):
        return
    host = str(addon.get("mqtt_host", "127.0.0.1")).strip()
    port = int(addon.get("mqtt_port", 1883))
    user = (addon.get("mqtt_user") or "").strip()
    password = (addon.get("mqtt_password") or "").strip()
    if not host:
        return
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1, client_id="motorline_mconnect_share_addon")
    if user:
        client.username_pw_set(user, password or None)

    def on_connect(c, userdata, flags, reason_code):
        if reason_code != 0:
            return
        device = {"identifiers": ["motorline_mconnect_share"], "name": "Motorline MConnect Share"}
        client.publish(f"{MQTT_DISCOVERY_PREFIX}/sensor/motorline_mconnect_share_estado/config", json.dumps({"name": "Estado", "state_topic": MQTT_TOPIC_STATE, "value_template": "{{ value_json.state }}", "unique_id": "motorline_mconnect_share_estado", "device": device}), retain=True)
        client.publish(f"{MQTT_DISCOVERY_PREFIX}/button/motorline_mconnect_share_abrir/config", json.dumps({"name": "Abrir", "command_topic": MQTT_TOPIC_COMMAND, "payload_press": "OPEN", "unique_id": "motorline_mconnect_share_abrir", "device": device}), retain=True)
        client.publish(f"{MQTT_DISCOVERY_PREFIX}/button/motorline_mconnect_share_fechar/config", json.dumps({"name": "Fechar", "command_topic": MQTT_TOPIC_COMMAND, "payload_press": "CLOSE", "unique_id": "motorline_mconnect_share_fechar", "device": device}), retain=True)
        client.subscribe(MQTT_TOPIC_COMMAND)

    def on_message(c, userdata, msg):
        opts = load_options()
        device_id = (opts.get("device_id") or "").strip()
        if not device_id:
            return
        try:
            payload = (msg.payload or b"").decode("utf-8").strip().upper()
        except Exception:
            payload = "OPEN"
        value = 0 if payload in ("CLOSE", "0", "FECHAR") else 2
        set_device_value(device_id, value)
        _mqtt_publish_state(c)

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(host, port, 60)
    except Exception as e:
        logger.warning("MQTT connect falhou: %s", e)
        return
    client.loop_start()
    # Primeira publicação de estado após ligar
    try:
        time.sleep(2)
        _mqtt_publish_state(client)
    except Exception as e:
        logger.debug("MQTT publish inicial: %s", e)
    # Loop com intervalo dinâmico: rápido após comando recente, lento em repouso
    while True:
        try:
            state = load_state()
            last_cmd = float(state.get("last_command_at") or 0)
        except Exception:
            last_cmd = 0.0
        now = time.time()
        interval = MQTT_STATE_IDLE_INTERVAL
        if last_cmd and (now - last_cmd) < 60:
            interval = MQTT_STATE_ACTIVE_INTERVAL
        time.sleep(interval)
        try:
            _mqtt_publish_state(client)
        except Exception as e:
            logger.debug("MQTT publish: %s", e)


# ----- Routes -----
@app.route("/api/ui-state", methods=["GET"])
def api_ui_state():
    opts = load_options()
    device_id = (opts.get("device_id") or "").strip()
    token_str = (opts.get("token") or "").strip()
    expires_at = _token_expires_at or float(opts.get("token_expires_at") or 0)
    token, msg = ensure_token()
    if token and not device_id:
        did = get_first_gate_device_id(token)
        if did:
            save_state({"device_id": did})
            device_id = did
    status = "ready" if token else "not_logged_in"
    out = {
        "status": status,
        "message": msg or "",
        "guest_activated": bool(opts.get("guest_home_id") and opts.get("guest_access_code")),
        "device_id": device_id or None,
        "token_preview": (token_str[:32] + "…") if len(token_str) > 32 else (token_str or None),
        "token_expires_at": expires_at if expires_at > 0 else None,
        "token_expires_at_formatted": _format_token_expiry(expires_at),
    }
    if device_id and token:
        gate = get_gate_state(device_id, token)
        if gate is not None:
            out["gate_state"] = gate.get("value", 0)
            out["gate_state_state"] = gate.get("state", "desconhecido")
            out["gate_state_unit"] = gate.get("unit", "%")
    return jsonify(out)


@app.route("/api/guest/activate", methods=["POST"])
def api_guest_activate():
    data = request.get_json(silent=True) or {}
    shareable_link = (data.get("shareable_link") or data.get("link") or "").strip()
    home_id = (data.get("home_id") or "").strip()
    access_code = (data.get("access_code") or "").strip()
    if shareable_link:
        home_id, access_code = parse_shareable_link(shareable_link)
    if not home_id or not access_code:
        return jsonify({"ok": False, "error": "Envie shareable_link (URL) ou home_id e access_code."}), 400
    token, expires_in = guest_exchange_link_for_token(home_id, access_code)
    if not token:
        # Distinguir caso de limite de dispositivos vs link realmente inválido
        now = time.time()
        global _last_guest_exchange_max_devices_at
        if _last_guest_exchange_max_devices_at and (now - _last_guest_exchange_max_devices_at) < GUEST_MAX_DEVICES_COOLDOWN_SECONDS:
            return jsonify({
                "ok": False,
                "error": "O link é válido, mas o serviço recusou novas sessões (MaxTrustedDevicesError). Termine sessões noutros dispositivos/app MConnect ou remova partilhas antigas e tente de novo.",
            }), 400
        return jsonify({"ok": False, "error": "Link inválido ou expirado. Gere um novo link de partilha na app."}), 400
    global _token, _token_expires_at
    with _lock:
        _token = token
        _token_expires_at = time.time() + expires_in
    save_state({"guest_home_id": home_id, "guest_access_code": access_code, "token": _token, "token_expires_at": _token_expires_at})
    device_id = get_first_gate_device_id(_token)
    if device_id:
        save_state({"device_id": device_id})
        logger.info("Acesso por partilha ativado (home_id=%s, device_id=%s).", home_id[:12], device_id[:12])
    else:
        logger.info("Acesso por partilha ativado (home_id=%s). device_id não encontrado em /rooms.", home_id[:12])
    return jsonify({"ok": True, "message": "Acesso ativado. Pode usar o portão.", "expires_in": expires_in, "device_id": device_id})


@app.route("/api/device_id", methods=["POST"])
def api_set_device_id():
    data = request.get_json(silent=True) or {}
    did = (data.get("device_id") or "").strip()
    if not did:
        return jsonify({"ok": False, "error": "device_id em falta"}), 400
    save_state({"device_id": did})
    return jsonify({"ok": True, "device_id": did})


@app.route("/api/gate-state", methods=["GET"])
def api_gate_state():
    opts = load_options()
    device_id = (opts.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "device_id não configurado"}), 400
    token, err = ensure_token()
    if not token:
        return jsonify({"ok": False, "error": err or "Não autenticado"}), 401
    gate = get_gate_state(device_id, token)
    if gate is None:
        return jsonify({"ok": False, "error": "Dispositivo ou gate_state não encontrado"}), 404
    return jsonify({"ok": True, "value": gate.get("value", 0), "state": gate.get("state", "desconhecido"), "unit": gate.get("unit", "%")})


@app.route("/api/devices", methods=["GET"])
def api_devices():
    token, err = ensure_token()
    if not token:
        return jsonify({"ok": False, "error": err or "Não autenticado"}), 401
    return jsonify({"ok": True, "devices": get_devices(token)})


@app.route("/trigger", methods=["GET", "POST"])
@app.route("/command", methods=["GET", "POST"])
def trigger():
    opts = load_options()
    device_id = (opts.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "device_id não configurado"}), 400
    value = 1
    if request.is_json and request.json:
        value = request.json.get("value", 1)
    elif request.args.get("value") is not None:
        try:
            value = int(request.args.get("value"))
        except ValueError:
            value = request.args.get("value")
    ok, err = set_device_value(device_id, value)
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": err}), 502


@app.route("/device/value", methods=["PUT", "POST"])
def device_value():
    opts = load_options()
    device_id = (opts.get("device_id") or "").strip() or request.args.get("device_id")
    if not device_id:
        return jsonify({"ok": False, "error": "device_id em falta"}), 400
    value = (request.get_json(silent=True) or {}).get("value", 1)
    ok, err = set_device_value(device_id, value)
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": err}), 502


def _panel_html() -> str:
    return """<!DOCTYPE html>
<html lang="pt">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Portão MConnect — HA</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; max-width: 480px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; margin-bottom: 1rem; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 1.25rem; margin-bottom: 1rem; }
    .success { background: #d4edda; border-color: #28a745; }
    input, button { padding: 0.5rem 0.75rem; font-size: 1rem; }
    input { width: 100%; margin-bottom: 0.75rem; }
    button { background: #0d6efd; color: #fff; border: none; border-radius: 6px; cursor: pointer; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    #msg { margin-top: 0.5rem; min-height: 1.2rem; }
    .error { color: #721c24; font-size: 0.9rem; }
  </style>
</head>
<body>
  <h1>Portão MConnect no Home Assistant</h1>
  <div id="panel" class="card">A carregar...</div>
  <div id="msg"></div>
  <script>
    function el(id) { return document.getElementById(id); }
    function showMsg(text, isError) { var m = el('msg'); m.textContent = text || ''; m.className = isError ? 'error' : ''; }
    function setPanel(html) { el('panel').innerHTML = html; }
    function setPanelClass(c) { el('panel').className = 'card ' + (c || ''); }
    var activePollId = null;
    var passivePollId = null;
    function startActivePoll() {
      if (activePollId) return;
      if (passivePollId) { clearInterval(passivePollId); passivePollId = null; }
      activePollId = setInterval(poll, 5000);
    }
    function renderGuestConfig(msg) {
      msg = msg || '<p><strong>Configuração única</strong></p><p>Na app MConnect (telemóvel): Partilhar acesso → criar link de partilha. Cole esse link aqui e clique em Configurar. Depois o portão fica disponível no HA (este painel, MQTT, API) sem login nem códigos por email.</p>';
      setPanel('<div id="guestMsg">' + msg + '</div><input type="text" id="guestLinkInput" placeholder="https://mconnect.pt/shareable_link?home_id=...&access_code=..." style="width:100%;margin:0.5rem 0;"><button type="button" id="btnGuestActivate">Configurar</button>');
      el('btnGuestActivate').onclick = activateLink;
      if (el('guestLinkInput')) el('guestLinkInput').onkeydown = function(e) { if (e.key === 'Enter') activateLink(); };
    }
    function activateLink() {
      var link = (el('guestLinkInput') && el('guestLinkInput').value || '').trim();
      if (!link) { showMsg('Cole o link de partilha.', true); return; }
      showMsg('A configurar…');
      fetch('/api/guest/activate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ shareable_link: link }) })
        .then(r => r.json()).then(function(res) {
          showMsg(res.ok ? 'Configurado. A obter estado do portão…' : (res.error || 'Erro'), !res.ok);
          if (res.ok) {
            // Após registar o link, faz polling rápido até obter um estado real
            startActivePoll();
            poll();
          }
        })
        .catch(function() { showMsg('Erro de rede.', true); });
    }
    function poll() {
      fetch('/api/ui-state').then(r => r.json()).then(function(d) {
        if (d.status === 'ready') {
          setPanelClass('success');
          var html = '<p><strong>Portão</strong></p><p>Estado: <strong>' + (d.gate_state_state || '—') + '</strong></p>';
          html += '<p><button type="button" id="btnOpen">Abrir</button> <button type="button" id="btnClose" style="background:#6c757d;">Fechar</button></p>';
          html += '<p style="margin-top:0.75rem;"><button type="button" id="btnChangeLink" style="background:#6c757d;">Alterar link de partilha</button></p>';
          setPanel(html);
          if (el('btnOpen')) el('btnOpen').onclick = function() { fetch('/trigger', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"value":2}' }).then(r => r.json()).then(function(res) { showMsg(res.ok ? 'Abrir enviado.' : (res.error || 'Erro'), !res.ok); if (res.ok) startActivePoll(); poll(); }); };
          if (el('btnClose')) el('btnClose').onclick = function() { fetch('/trigger', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"value":0}' }).then(r => r.json()).then(function(res) { showMsg(res.ok ? 'Fechar enviado.' : (res.error || 'Erro'), !res.ok); if (res.ok) startActivePoll(); poll(); }); };
          if (el('btnChangeLink')) el('btnChangeLink').onclick = function() { renderGuestConfig(); };
          // Se ainda está "desconhecido" logo após ativar o link, manter polling rápido
          if (d.gate_state_state === 'desconhecido') {
            if (!activePollId) startActivePoll();
          } else if (d.gate_state_state === 'fechado') {
            // Quando o portão volta a ficar fechado, volta ao polling espaçado
            if (activePollId) { clearInterval(activePollId); activePollId = null; }
            if (!passivePollId) passivePollId = setInterval(poll, 1800000);
          }
          return;
        }
        setPanelClass('');
        var msg = !d.guest_activated
          ? '<p><strong>Configuração única</strong></p><p>Na app MConnect (telemóvel): Partilhar acesso → criar link de partilha. Cole esse link aqui e clique em Configurar. Depois o portão fica disponível no HA (este painel, MQTT, API) sem login nem códigos por email.</p>'
          : '<p>Link expirado ou inválido. Crie um novo link de partilha na app e cole aqui.</p>';
        var existingInput = el('guestLinkInput');
        if (existingInput) {
          el('guestMsg').innerHTML = msg;
          el('btnGuestActivate').onclick = activateLink;
          if (el('guestLinkInput')) el('guestLinkInput').onkeydown = function(e) { if (e.key === 'Enter') activateLink(); };
        } else {
          renderGuestConfig(msg);
        }
      }).catch(function() { setPanel('<p>Erro a obter estado.</p>'); });
    }
    poll();
    passivePollId = setInterval(poll, 1800000);
  </script>
</body>
</html>"""


@app.route("/")
def index():
    return _panel_html(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    opts = load_options()
    if opts.get("token"):
        _token = opts["token"]
        _token_expires_at = float(opts.get("token_expires_at") or 0)
    port = int(os.environ.get("PORT", 8766))
    logger.info("Motorline MConnect Share a iniciar na porta %s", port)
    t = threading.Thread(target=_mqtt_thread, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
