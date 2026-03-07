#!/usr/bin/env python3
from __future__ import annotations

"""
Addon Home Assistant: proxy para API Motorline MConnect.
Login automático ao arranque, painel para código por email, renovação e verificação horária do token.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

import requests

try:
    import fcntl
except ImportError:
    fcntl = None
from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Tudo em estado persistente (sem configuração no HA). API URL e refresh são internos.
DATA_DIR = Path("/data")
STATE_PATH = DATA_DIR / "motorline_state.json"
if not DATA_DIR.exists():
    DATA_DIR = Path(__file__).parent
    STATE_PATH = DATA_DIR / "motorline_state.json"

# Auth: rest.mconnect.pt (HAR). Comando do portão: api.mconnect.motorline.pt (testado pelo utilizador).
API_BASE_URL = "https://rest.mconnect.pt"
DEVICES_API_BASE_URL = "https://api.mconnect.motorline.pt"
REFRESH_BEFORE_EXPIRY_SECONDS = 300

app = Flask(__name__)

# Estado do token (em memória; renovado ao receber 401 ou antes do expiry)
_token = None  # token da casa (rest) ou user (fallback para api dispositivos)
_token_expires_at = 0.0
_user_token = None  # token de utilizador (fallback para api.mconnect.motorline.pt se home der 401)
_user_token_expires_at = 0.0

# Login em 2 fases: quando a API envia código por email
_awaiting_code = False
_login_session = None  # {"api_base_url", "email"}

# Alerta para o painel: sessão expirou e é preciso novo código (verificação horária)
_token_expired_alert = False
_lock = threading.Lock()
# Evitar vários logins em paralelo (vários emails): lock em ficheiro (vale entre threads e processos)
_LOGIN_LOCK_PATH = DATA_DIR / ".motorline_login.lock"


def load_options():
    """Configuração vem do state (email, password, device_id, token)."""
    state = load_state()
    return {
        "api_base_url": API_BASE_URL,
        "refresh_before_expiry_seconds": REFRESH_BEFORE_EXPIRY_SECONDS,
        "email": (state.get("email") or "").strip(),
        "password": state.get("password") or "",
        "device_id": (state.get("device_id") or "").strip(),
        "token": (state.get("token") or "").strip() or None,
        "token_expires_at": state.get("token_expires_at") or 0,
    }


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


# UUID fixo para o addon (API MConnect exige em /user/mfa/verify)
MFA_DEVICE_UUID = "motorline-mconnect-addon-1"


def _is_mfa_required_response(status_code: int, data: dict) -> bool:
    """Deteta se a API pediu MFA (código por email) em vez de token."""
    if status_code == 401:
        return True
    if status_code not in (200, 202, 204) or not isinstance(data, dict):
        return False
    if data.get("access_token") or data.get("token") or data.get("accessToken"):
        return False
    if data.get("mfa_required") or data.get("mfa") is True:
        return True
    msg = (data.get("message") or data.get("msg") or "").lower()
    if any(x in msg for x in ("mfa", "code", "codigo", "código", "verification", "email")):
        return True
    return False


def login(api_base_url: str, email: str, password: str) -> tuple[str | None, int]:
    """
    Faz login na API Motorline (api.mconnect.motorline.pt).
    Endpoint: POST /auth/token com grant_type=authorization, email, password, mfa=true.
    Se a API exigir MFA, define awaiting_code e retorna (None, 0); o utilizador submete o código em /login/verify.
    """
    global _awaiting_code, _login_session

    base = api_base_url.rstrip("/")
    url = f"{base}/auth/token"
    body = {
        "grant_type": "authorization",
        "email": email,
        "password": password,
        "mfa": True,
    }
    headers = {"Content-Type": "application/json"}

    try:
        r = requests.post(url, json=body, headers=headers, timeout=15)
        data = r.json() if r.text else {}
        token = data.get("access_token") or data.get("token") or data.get("accessToken")
        if r.status_code == 200 and token:
            if data.get("mfa_required") or data.get("requires_verification") or data.get("mfa") is True:
                _awaiting_code = True
                _login_session = {"api_base_url": base, "email": email}
                for key in ("session_id", "request_id", "mfa_token", "mfa_request_id", "state", "nonce", "session"):
                    if data.get(key) is not None:
                        _login_session[key] = data[key]
                logger.info("API devolveu token mas ainda exige MFA. Introduza o código do email.")
                return None, 0
            # Token "direto" é muitas vezes MFA token (API dispositivos rejeita com "The token is a MFA token")
            home_token, home_expires, _ = exchange_user_token_for_home_token(token)
            if home_token:
                logger.info("Login OK (token da casa), expira em %s s", home_expires)
                return home_token, home_expires
            expires_in = int(data.get("expires_in", data.get("expiresIn", 3600)))
            logger.info("Token do login é MFA token; a API do portão exige código. Introduza o código do email.")
            _awaiting_code = True
            _login_session = {"api_base_url": base, "email": email}
            for key in ("session_id", "request_id", "mfa_token", "mfa_request_id", "state", "nonce", "session"):
                if data.get(key) is not None:
                    _login_session[key] = data[key]
            return None, 0
        if r.status_code == 401 or _is_mfa_required_response(r.status_code, data):
            _awaiting_code = True
            _login_session = {"api_base_url": base, "email": email}
            for key in ("session_id", "request_id", "mfa_token", "mfa_request_id", "state", "nonce", "session"):
                if data.get(key) is not None:
                    _login_session[key] = data[key]
            logger.info("MFA exigido. Resposta API: %s. Use POST /login/verify com o código.", data)
            return None, 0
        logger.warning("Login inesperado: status=%s body=%s", r.status_code, data)
    except Exception as e:
        logger.debug("Login /auth/token falhou: %s", e)

    logger.error("Login falhou")
    return None, 0


def verify_code(code: str) -> tuple[str | None, int]:
    """
    Completa o login MFA com o código recebido por email.
    Se não houver sessão MFA ativa (ex.: API devolveu "token direto" que não serve para o portão),
    usa as credenciais guardadas para tentar na mesma o verify – a API pode aceitar email + código.
    """
    global _awaiting_code, _login_session

    if not _login_session and _awaiting_code:
        logger.error("Nenhum login à espera de código. Faça primeiro login (email/password) e espere o email.")
        return None, 0

    if not _login_session:
        opts = load_options()
        if not opts.get("email") or not opts.get("password"):
            logger.error("Sem credenciais guardadas. Introduza email e password no painel primeiro.")
            return None, 0
        _login_session = {"api_base_url": opts.get("api_base_url", API_BASE_URL), "email": opts.get("email", "")}

    base = _login_session.get("api_base_url", API_BASE_URL).rstrip("/")
    code_clean = code.strip().replace(" ", "")
    payload = {
        "code": code_clean,
        "otp": code_clean,
        "platform": "HomeAssistant",
        "model": "addon",
        "uuid": MFA_DEVICE_UUID,
    }
    if _login_session.get("email"):
        payload["email"] = _login_session["email"]
    for key in ("session_id", "request_id", "mfa_token", "mfa_request_id", "state", "nonce", "session"):
        if _login_session.get(key) is not None:
            payload[key] = _login_session[key]

    def try_verify(url: str, body: dict) -> tuple[str | None, int]:
        try:
            r = requests.post(url, json=body, headers={"Content-Type": "application/json"}, timeout=15)
            data = r.json() if r.text else {}
            token = data.get("access_token") or data.get("token") or data.get("accessToken")
            if r.status_code == 200 and token:
                exp = int(data.get("expires_in", data.get("expiresIn", 3600)))
                return token, exp
            logger.warning("MFA verify %s: status=%s body=%s", url, r.status_code, data)
        except Exception as e:
            logger.debug("MFA verify %s: %s", url, e)
        return None, 0

    for url in (f"{base}/user/mfa/verify", f"{base}/auth/mfa/verify", f"{base}/mfa/verify"):
        token, exp = try_verify(url, payload)
        if token:
            _awaiting_code = False
            _login_session = None
            logger.info("MFA verificado, token obtido (expira em %s s)", exp)
            return token, exp

    email = _login_session.get("email", "")
    pwd = load_options().get("password", "")
    if pwd:
        token_body = {"grant_type": "authorization", "email": email, "password": pwd, "mfa_code": code_clean}
        for url in (f"{base}/auth/token", f"{base}/oauth/token"):
            token, exp = try_verify(url, token_body)
            if token:
                _awaiting_code = False
                _login_session = None
                logger.info("MFA via auth/token, token obtido")
                return token, exp

    return None, 0


def exchange_user_token_for_home_token(user_token: str) -> tuple[str | None, int, str | None]:
    """
    Na API rest.mconnect.pt: o token de /user/mfa/verify é de utilizador.
    Para comandar dispositivos é preciso o token da "casa": GET /homes → POST /homes/auth/token.
    Retorna (home_access_token, expires_in, home_id) ou (None, 0, None).
    """
    base = API_BASE_URL.rstrip("/")
    headers = {"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"}
    try:
        r = requests.get(f"{base}/homes", headers=headers, timeout=15)
        if r.status_code != 200:
            logger.warning("GET /homes: status=%s %s", r.status_code, r.text[:200])
            return None, 0, None
        homes = r.json() if r.text else []
        if not isinstance(homes, list) or not homes:
            logger.warning("GET /homes: sem casas")
            return None, 0, None
        home_id = homes[0].get("_id") or homes[0].get("id") or ""
        if not home_id:
            return None, 0, None
        r2 = requests.post(
            f"{base}/homes/auth/token",
            json={"grant_type": "authorization", "code": user_token, "home_id": home_id},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        data = r2.json() if r2.text else {}
        token = data.get("access_token") or data.get("token") or data.get("accessToken")
        if r2.status_code != 200 or not token:
            logger.warning("POST /homes/auth/token: status=%s body=%s", r2.status_code, data)
            return None, 0, None
        expires_in = int(data.get("expires_in", data.get("expiresIn", 3600)))
        logger.info("Token da casa obtido (home_id=%s, expira em %s s)", home_id[:12], expires_in)
        return token, expires_in, home_id
    except Exception as e:
        logger.warning("exchange_user_token_for_home_token: %s", e)
        return None, 0, None


def get_devices(token: str) -> list[dict]:
    """Lista dispositivos. Na rest.mconnect.pt vêm em GET /rooms (cada room tem devices)."""
    base = API_BASE_URL.rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = requests.get(f"{base}/rooms", headers=headers, timeout=15)
        if r.status_code == 200 and r.text:
            raw = r.json()
            rooms = raw if isinstance(raw, list) else []
            out = []
            for room in rooms:
                for d in room.get("devices", []):
                    if isinstance(d, dict):
                        out.append({"id": d.get("_id", d.get("id", d.get("device_id", ""))), "name": d.get("name", d.get("label", ""))})
            if out:
                return out
    except Exception as e:
        logger.debug("get_devices /rooms: %s", e)
    for path in ("/devices", "/user/devices"):
        try:
            r = requests.get(f"{base}{path}", headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            data = r.json() if r.text else {}
            items = data if isinstance(data, list) else data.get("devices", data.get("data", []))
            if isinstance(items, list) and items:
                return [{"id": d.get("id", d.get("device_id", d.get("_id", ""))) if isinstance(d, dict) else str(d), "name": d.get("name", d.get("label", "")) if isinstance(d, dict) else ""} for d in items]
        except Exception as e:
            logger.debug("get_devices %s: %s", path, e)
    return []


def _acquire_login_file_lock():
    """Lock exclusivo em ficheiro (entre threads e processos). Retorna fd ou None se já houver login a correr."""
    if not fcntl:
        return None
    path = _LOGIN_LOCK_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, BlockingIOError):
        return None

def _release_login_file_lock(fd):
    if fd is None:
        return
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except Exception:
        pass

def ensure_token() -> tuple[str | None, str]:
    """Retorna o token se válido (memória ou state). Nunca chama login() — o código só é pedido quando o utilizador clica em 'Pedir código por email'."""
    global _token, _token_expires_at
    if _awaiting_code:
        return None, "À espera do código por email"
    opts = load_options()
    refresh_before = int(opts.get("refresh_before_expiry_seconds", REFRESH_BEFORE_EXPIRY_SECONDS))
    now = time.time()
    if _token and _token_expires_at > now + refresh_before:
        return _token, ""
    # Hidratar a partir do state (ex.: após reinício do addon)
    persisted = opts.get("token")
    persisted_exp = float(opts.get("token_expires_at") or 0)
    if persisted and persisted_exp > now + refresh_before:
        _token, _token_expires_at = persisted, persisted_exp
        return _token, ""
    if not (opts.get("email") or "").strip() or not opts.get("password"):
        return None, "Introduza email e password no painel"
    return None, "Abra o painel e clique em 'Pedir código por email' para obter um novo código."


def _background_tasks():
    """A cada hora: verifica se o token expirou e ativa o alerta no painel. Nunca envia email (só o utilizador com o botão)."""
    global _token_expired_alert
    time.sleep(2)
    logger.info("Verificação de token em background ativa (intervalo: 1 h); o código só é pedido ao clicar no botão no painel.")
    while True:
        time.sleep(3600)
        now = time.time()
        if (not _token or _token_expires_at <= now) and not _awaiting_code:
            _token_expired_alert = True
            logger.warning("Token expirado. Abra o painel e clique em 'Pedir código por email' para obter um novo código.")


def _post_device_value(device_id: str, num: int, token: str, auth_scheme: str = "Jwt") -> tuple[bool, int, str]:
    """Faz POST para /devices/value. Retorna (ok, status_code, erro ou '')."""
    base = DEVICES_API_BASE_URL.rstrip("/")
    url = f"{base}/devices/value/{device_id}"
    headers = {
        "Authorization": f"{auth_scheme} {token}",
        "Content-Type": "application/json",
        "Timezone": "Europe/Lisbon",
    }
    r = requests.post(url, json={"value_id": "gate_state", "value": num}, headers=headers, timeout=15)
    if r.status_code in (200, 204):
        return True, r.status_code, ""
    return False, r.status_code, r.text[:300] if r.text else ""


def set_device_value(device_id: str, value: str | int | float) -> tuple[bool, str]:
    """
    Define o valor do dispositivo (portão). Endpoint em api.mconnect.motorline.pt.
    Tenta primeiro com token da casa (Jwt); em 401 tenta com token de utilizador (Jwt).
    """
    global _token, _token_expires_at
    base = DEVICES_API_BASE_URL.rstrip("/")
    num = 2 if value in (1, "1", "open") else int(value) if isinstance(value, (int, float)) else 2

    token, error_msg = ensure_token()
    if not token:
        return False, error_msg or "Falha ao obter token"

    # 1) Tentar com token principal (casa) — Jwt
    ok, status, err = _post_device_value(device_id, num, token, "Jwt")
    if ok:
        return True, ""
    if status != 401:
        logger.warning("devices/value: HTTP %s %s", status, err)
        return False, f"HTTP {status}: {err}"

    logger.warning("devices/value 401 (Jwt). Resposta: %s", err)
    ok, status, _ = _post_device_value(device_id, num, token, "Bearer")
    if ok:
        return True, ""

    # 2) Fallback: token de utilizador
    if _user_token and _user_token_expires_at > time.time():
        ok, status2, err2 = _post_device_value(device_id, num, _user_token, "Jwt")
        if ok:
            return True, ""
        logger.warning("devices/value 401 também com user_token. Resposta: %s", err2)

    # Não limpar _token: o painel continua "Operacional"; o utilizador vê o erro e pode clicar em "Pedir código por email" quando quiser.
    return False, "401 na API do portão — token expirado ou inválido. Clique em 'Pedir código por email' no painel para obter um novo."


@app.route("/api/ui-state", methods=["GET"])
def api_ui_state():
    """Estado para o painel: status, has_credentials, token_expired_alert, device_id."""
    opts = load_options()
    device_id = (opts.get("device_id") or "").strip()
    status_res = login_status().get_json()
    has_credentials = bool((opts.get("email") or "").strip() and opts.get("password"))
    return jsonify({
        "status": status_res["status"],
        "message": status_res["message"],
        "token_expired_alert": status_res.get("token_expired_alert", False),
        "device_id": device_id or None,
        "has_credentials": has_credentials,
    })


@app.route("/api/devices", methods=["GET"])
def api_devices():
    """Lista dispositivos (requer token)."""
    token, err = ensure_token()
    if not token:
        return jsonify({"ok": False, "error": err or "Não autenticado"}), 401
    devices = get_devices(token)
    return jsonify({"ok": True, "devices": devices})


@app.route("/api/device_id", methods=["POST"])
def api_set_device_id():
    """Guarda o device_id no estado persistente. Body: {\"device_id\": \"...\"}."""
    data = request.get_json(silent=True) or {}
    did = (data.get("device_id") or "").strip()
    if not did:
        return jsonify({"ok": False, "error": "device_id em falta"}), 400
    save_state({"device_id": did})
    return jsonify({"ok": True, "device_id": did})


def _panel_html() -> str:
    html = """<!DOCTYPE html>
<html lang="pt">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Motorline MConnect</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; max-width: 480px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; margin-bottom: 1rem; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 1.25rem; margin-bottom: 1rem; }
    .alert { background: #fff3cd; border-color: #ffc107; }
    .success { background: #d4edda; border-color: #28a745; }
    .error { color: #721c24; font-size: 0.9rem; margin-top: 0.5rem; }
    input, button { padding: 0.5rem 0.75rem; font-size: 1rem; }
    input { width: 100%; margin-bottom: 0.75rem; }
    button { background: #0d6efd; color: #fff; border: none; border-radius: 6px; cursor: pointer; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    #msg { margin-top: 0.5rem; min-height: 1.2rem; }
  </style>
</head>
<body>
  <h1>Motorline MConnect</h1>
  <div id="panel" class="card">A carregar...</div>
  <div id="msg"></div>
  <script>
    function el(id) { return document.getElementById(id); }
    function showMsg(text, isError) {
      var m = el('msg');
      m.textContent = text || '';
      m.className = isError ? 'error' : '';
    }
    function setPanel(html) { el('panel').innerHTML = html; }
    function setPanelClass(c) { el('panel').className = 'card ' + (c || ''); }

    function poll() {
      fetch('/api/ui-state').then(r => r.json()).then(function(d) {
        if (d.status === 'ready') {
          if (!d.device_id && el('deviceIdInput')) return;
          setPanelClass('success');
          var html = '<p><strong>Operacional</strong></p>';
          if (d.device_id) {
            html += '<p>Dispositivo: <code>' + (d.device_id.length > 20 ? d.device_id.slice(0,12)+'…' : d.device_id) + '</code></p>';
            html += '<p><button type="button" id="btnTrigger">Disparar portão</button></p>';
          } else {
            html += '<p class="alert" style="margin:0 0 0.75rem 0; padding:0.5rem;">ID do dispositivo não foi obtido. Introduza-o abaixo (ex: 66755146c8a511e8645bd710). Pode encontrá-lo na app Motorline ou no URL do dispositivo.</p>';
            html += '<input type="text" id="deviceIdInput" placeholder="ID do dispositivo (device_id)" style="margin-bottom:0.5rem;">';
            html += '<button type="button" id="btnSaveDevice">Guardar ID</button>';
          }
          setPanel(html);
          if (el('btnTrigger')) el('btnTrigger').onclick = triggerGate;
          if (el('btnSaveDevice')) { el('btnSaveDevice').onclick = saveDeviceId; el('deviceIdInput').onkeydown = function(e) { if (e.key === 'Enter') saveDeviceId(); }; }
          return;
        }
        if (d.status === 'awaiting_code') {
          if (el('code') && el('btnVerify')) return;
          setPanelClass(d.token_expired_alert ? 'alert' : '');
          setPanel('<p><strong>Foi enviado um código ao seu email.</strong> Introduza-o abaixo e clique em Submeter código.</p>' +
            '<input type="text" id="code" placeholder="Código (ex: 123456)" maxlength="8" autocomplete="one-time-code">' +
            '<button type="button" id="btnVerify">Submeter código</button>');
          el('btnVerify').onclick = submitCode;
          if (el('code')) el('code').onkeydown = function(e) { if (e.key === 'Enter') submitCode(); };
          return;
        }
        if (!d.has_credentials) {
          if (el('email') && el('password')) return;
          setPanelClass('');
          setPanel('<p><strong>Primeiro uso:</strong> introduza o email e a password da sua conta Motorline MConnect.</p>' +
            '<input type="email" id="email" placeholder="Email" autocomplete="email">' +
            '<input type="password" id="password" placeholder="Password" autocomplete="current-password">' +
            '<button type="button" id="btnLogin">Iniciar sessão</button>');
          el('btnLogin').onclick = submitLogin;
          if (el('email')) el('email').onkeydown = function(e) { if (e.key === 'Enter') el('password') && el('password').focus(); };
          if (el('password')) el('password').onkeydown = function(e) { if (e.key === 'Enter') submitLogin(); };
          return;
        }
        if (el('code') && el('btnVerify')) return;
        setPanelClass('');
        setPanel('<p>' + (d.message || 'Não autenticado') + '</p>' +
          '<p><strong>O código só é enviado quando clicar no botão abaixo.</strong> Depois de receber o email, introduza o código:</p>' +
          '<p><button type="button" id="btnStart">Pedir código por email</button></p>' +
          '<input type="text" id="code" placeholder="Código (ex: 123456)" maxlength="8" autocomplete="one-time-code">' +
          '<button type="button" id="btnVerify">Submeter código</button>');
        el('btnVerify').onclick = submitCode;
        if (el('code')) el('code').onkeydown = function(e) { if (e.key === 'Enter') submitCode(); };
        el('btnStart').onclick = requestCode;
      }).catch(function() { setPanel('<p>Erro a obter estado. A recarregar...</p>'); });
    }
    function showCodeForm() {
      setPanelClass('');
      setPanel('<p><strong>Foi enviado um código ao seu email.</strong> Introduza-o abaixo para concluir o login.</p>' +
        '<input type="text" id="code" placeholder="Código (ex: 123456)" maxlength="8" autocomplete="one-time-code">' +
        '<button type="button" id="btnVerify">Submeter código</button>');
      el('btnVerify').onclick = submitCode;
      if (el('code')) { el('code').focus(); el('code').onkeydown = function(e) { if (e.key === 'Enter') submitCode(); }; }
    }
    function submitLogin() {
      var email = (el('email') && el('email').value || '').trim();
      var password = el('password') && el('password').value || '';
      if (!email) { showMsg('Introduza o email.', true); return; }
      if (!password) { showMsg('Introduza a password.', true); return; }
      showMsg('A iniciar sessão...');
      fetch('/login/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email, password: password })
      }).then(r => r.json()).then(function(data) {
        showMsg('');
        if (data.status === 'awaiting_code') showCodeForm();
        else poll();
      }).catch(function() { showMsg('Erro de rede.', true); });
    }
    function requestCode() {
      var btn = el('btnStart');
      if (btn) btn.disabled = true;
      fetch('/login/start', { method: 'POST' }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.status === 'awaiting_code') showCodeForm();
        else poll();
      }).finally(function() { if (btn) btn.disabled = false; });
    }
    function startLogin() { requestCode(); }
    function submitCode() {
      var code = (el('code') && el('code').value || '').trim();
      if (!code) { showMsg('Introduza o código.', true); return; }
      showMsg('A verificar...');
      fetch('/login/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code })
      }).then(r => r.json()).then(function(res) {
        if (res.ok) { showMsg('Login concluído.'); poll(); }
        else { showMsg((res.error || 'Código inválido.') + (res.hint ? ' ' + res.hint : ''), true); }
      }).catch(function() { showMsg('Erro de rede.', true); });
    }
    function saveDeviceId() {
      var id = (el('deviceIdInput') && el('deviceIdInput').value || '').trim();
      if (!id) { showMsg('Introduza o ID do dispositivo.', true); return; }
      showMsg('A guardar...');
      fetch('/api/device_id', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: id })
      }).then(r => r.json()).then(function(res) {
        if (res.ok) { showMsg('ID guardado.'); poll(); }
        else { showMsg(res.error || 'Erro.', true); }
      }).catch(function() { showMsg('Erro de rede.', true); });
    }
    function triggerGate() {
      var btn = el('btnTrigger');
      if (btn) btn.disabled = true;
      fetch('/trigger', { method: 'POST' }).then(r => r.json()).then(function(res) {
        showMsg(res.ok ? 'Comando enviado.' : (res.error || 'Erro'), !res.ok);
      }).catch(function() { showMsg('Erro de rede.', true); }).finally(function() { if (btn) btn.disabled = false; });
    }
    poll();
    setInterval(poll, 4000);
  </script>
</body>
</html>"""
    return html


@app.route("/")
def index():
    """Painel do addon: login, código, estado operacional e alerta de sessão expirada."""
    return _panel_html(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/login/status", methods=["GET"])
def login_status():
    """Indica se o add-on está à espera do código de verificação por email."""
    global _awaiting_code, _token, _token_expired_alert
    if _awaiting_code:
        return jsonify({
            "status": "awaiting_code",
            "message": "Abra o email e introduza o código abaixo.",
            "token_expired_alert": _token_expired_alert,
        })
    if _token:
        return jsonify({"status": "ready", "message": "Sessão ativa", "token_expired_alert": False})
    return jsonify({"status": "not_logged_in", "message": "Faça login (ou aguarde renovação)"})


@app.route("/login/start", methods=["POST"])
def login_start():
    """Único ponto que pede código por email: chamado quando o utilizador clica em 'Pedir código por email' no painel."""
    global _token, _token_expires_at
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if email and password:
        save_state({"email": email, "password": password})
    opts = load_options()
    email = opts.get("email", "").strip()
    password = opts.get("password", "")
    if not email or not password:
        return jsonify({"status": "not_logged_in", "message": "Introduza email e password no painel", "token_expired_alert": _token_expired_alert}), 200
    fd = _acquire_login_file_lock()
    if fd is None:
        return jsonify({"status": "awaiting_code", "message": "Já foi enviado um email. Introduza o código ou aguarde.", "token_expired_alert": _token_expired_alert}), 200
    try:
        api_base_url = opts.get("api_base_url", API_BASE_URL)
        token, expires_in = login(api_base_url, email, password)
        with _lock:
            if token:
                _token = token
                _token_expires_at = time.time() + expires_in
    finally:
        _release_login_file_lock(fd)
    return login_status()


@app.route("/login/verify", methods=["POST"])
def login_verify():
    """
    Submete o código recebido por email para completar o login.
    Body JSON: {"code": "123456"} ou query ?code=123456
    Se não houver device_id configurado, obtém a lista de dispositivos e guarda o primeiro.
    """
    global _token, _token_expires_at, _token_expired_alert, _user_token, _user_token_expires_at
    code = None
    if request.is_json and request.json:
        code = (request.json.get("code") or request.json.get("otp") or "").strip()
    if not code:
        code = (request.args.get("code") or request.args.get("otp") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "Envie o código no body: {\"code\": \"123456\"}"}), 400

    user_token, expires_in = verify_code(code)
    if not user_token:
        return jsonify({
            "ok": False,
            "error": "Código inválido ou API não respondeu com token. O código pode ter expirado (vários minutos).",
            "hint": "Clique em 'Tentar novamente', espere o novo email e introduza o código de imediato. Consulte os logs do addon para detalhes da API.",
        }), 400

    home_token, home_expires, home_id = exchange_user_token_for_home_token(user_token)
    if home_token:
        _token = home_token
        _token_expires_at = time.time() + home_expires
        if home_id:
            save_state({"home_id": home_id})
    else:
        _token = user_token
        _token_expires_at = time.time() + expires_in
    _user_token = user_token
    _user_token_expires_at = time.time() + (home_expires if home_token else expires_in)
    _token_expired_alert = False
    save_state({"token": _token, "token_expires_at": _token_expires_at})

    opts = load_options()
    device_id = (opts.get("device_id") or "").strip()
    if not device_id:
        devices = get_devices(_token)
        if len(devices) == 1:
            device_id = devices[0].get("id", "")
            if device_id:
                save_state({"device_id": device_id})
                logger.info("device_id obtido automaticamente: %s", device_id)
        elif len(devices) > 1:
            save_state({"device_id": devices[0].get("id", "")})
            logger.info("Vários dispositivos; guardado o primeiro. Pode alterar em /api/device_id")

    return jsonify({"ok": True, "message": "Login concluído. Pode usar /trigger para o portão."})


@app.route("/trigger", methods=["GET", "POST"])
@app.route("/command", methods=["GET", "POST"])
def trigger():
    """
    Dispara o comando do portão.
    Body opcional: {"value": 1} ou query ?value=1. Valor default 1 (abrir/disparar).
    """
    opts = load_options()
    device_id = opts.get("device_id", "").strip()
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
    """Define valor do dispositivo; body JSON: {"value": ...}."""
    opts = load_options()
    device_id = opts.get("device_id", "").strip() or request.args.get("device_id")
    if not device_id:
        return jsonify({"ok": False, "error": "device_id em falta"}), 400

    data = request.get_json(silent=True) or {}
    value = data.get("value", 1)

    ok, err = set_device_value(device_id, value)
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": err}), 502


if __name__ == "__main__":
    # Carregar token persistido para sobreviver a reinícios
    opts = load_options()
    if opts.get("token") and (opts.get("token_expires_at") or 0) > time.time():
        _token = opts["token"]
        _token_expires_at = float(opts["token_expires_at"])
        logger.info("Token restaurado do state")
    port = int(os.environ.get("PORT", 8765))
    logger.info("A iniciar servidor na porta %s", port)
    t = threading.Thread(target=_background_tasks, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
