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

API_BASE_URL = "https://api.mconnect.motorline.pt"
REFRESH_BEFORE_EXPIRY_SECONDS = 300

app = Flask(__name__)

# Estado do token (em memória; renovado ao receber 401 ou antes do expiry)
_token = None
_token_expires_at = 0.0

# Login em 2 fases: quando a API envia código por email
_awaiting_code = False
_login_session = None  # {"api_base_url", "email"}

# Alerta para o painel: sessão expirou e é preciso novo código (verificação horária)
_token_expired_alert = False
_lock = threading.Lock()


def load_options():
    """Configuração vem do state (email, password, device_id). API URL e refresh são constantes."""
    state = load_state()
    return {
        "api_base_url": API_BASE_URL,
        "refresh_before_expiry_seconds": REFRESH_BEFORE_EXPIRY_SECONDS,
        "email": (state.get("email") or "").strip(),
        "password": state.get("password") or "",
        "device_id": (state.get("device_id") or "").strip(),
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
            expires_in = int(data.get("expires_in", data.get("expiresIn", 3600)))
            logger.info("Login OK (token direto), expira em %s s", expires_in)
            return token, expires_in
        if r.status_code == 401 or _is_mfa_required_response(r.status_code, data):
            _awaiting_code = True
            _login_session = {"api_base_url": base, "email": email}
            logger.info("MFA exigido. Código enviado por email. Use POST /login/verify com o código.")
            return None, 0
        logger.warning("Login inesperado: status=%s body=%s", r.status_code, data)
    except Exception as e:
        logger.debug("Login /auth/token falhou: %s", e)

    logger.error("Login falhou")
    return None, 0


def verify_code(code: str) -> tuple[str | None, int]:
    """
    Completa o login MFA com o código recebido por email.
    API Motorline: POST /user/mfa/verify com code, platform, model, uuid.
    Retorna (access_token, expires_in) ou (None, 0).
    """
    global _awaiting_code, _login_session

    if not _awaiting_code or not _login_session:
        logger.error("Nenhum login à espera de código. Faça primeiro login (email/password).")
        return None, 0

    base = _login_session["api_base_url"]
    url = f"{base}/user/mfa/verify"
    payload = {
        "code": code.strip(),
        "platform": "HomeAssistant",
        "model": "addon",
        "uuid": MFA_DEVICE_UUID,
    }
    try:
        r = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        data = r.json() if r.text else {}
        token = data.get("access_token") or data.get("token") or data.get("accessToken")
        if r.status_code == 200 and token:
            expires_in = int(data.get("expires_in", data.get("expiresIn", 3600)))
            _awaiting_code = False
            _login_session = None
            logger.info("MFA verificado, token obtido (expira em %s s)", expires_in)
            return token, expires_in
        logger.warning("MFA verify falhou: status=%s body=%s", r.status_code, data)
    except Exception as e:
        logger.debug("MFA verify falhou: %s", e)

    return None, 0


def get_devices(token: str) -> list[dict]:
    """Lista dispositivos da API (portões). Tenta GET /devices e GET /user/devices."""
    opts = load_options()
    base = opts.get("api_base_url", "https://api.mconnect.motorline.pt").rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for path in ("/devices", "/user/devices"):
        try:
            r = requests.get(f"{base}{path}", headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            data = r.json() if r.text else {}
            items = data if isinstance(data, list) else data.get("devices", data.get("data", []))
            if isinstance(items, list) and items:
                out = []
                for d in items:
                    if isinstance(d, dict):
                        out.append({"id": d.get("id", d.get("device_id", d.get("_id", ""))), "name": d.get("name", d.get("label", ""))})
                    else:
                        out.append({"id": str(d), "name": ""})
                return out
        except Exception as e:
            logger.debug("get_devices %s: %s", path, e)
    return []


def ensure_token() -> tuple[str | None, str]:
    """Obtém um token válido (renovando se necessário). Retorna (token, mensagem_erro)."""
    global _token, _token_expires_at
    if _awaiting_code:
        return None, "À espera do código por email"
    opts = load_options()
    refresh_before = int(opts.get("refresh_before_expiry_seconds", REFRESH_BEFORE_EXPIRY_SECONDS))
    now = time.time()

    if _token and _token_expires_at > now + refresh_before:
        return _token, ""

    api_base_url = opts.get("api_base_url", API_BASE_URL)
    email = opts.get("email", "")
    password = opts.get("password", "")
    if not email or not password:
        return None, "Introduza email e password no painel"

    token, expires_in = login(api_base_url, email, password)
    if not token:
        return None, "Login falhou (verifica credenciais ou consulta os logs)"

    _token = token
    _token_expires_at = now + expires_in
    return _token, ""


def _background_tasks():
    """Ao arranque: tenta login. A cada hora: verifica token e, se expirado, alerta para novo código."""
    global _token_expired_alert
    time.sleep(2)
    with _lock:
        ensure_token()
    logger.info("Verificação de token em background ativa (intervalo: 1 h)")
    while True:
        time.sleep(3600)
        with _lock:
            t, _ = ensure_token()
            if t is None and _awaiting_code:
                _token_expired_alert = True
                logger.warning("Token expirado ou inválido. É necessário novo código por email.")


def set_device_value(device_id: str, value: str | int | float) -> tuple[bool, str]:
    """
    Define o valor do dispositivo (portão) na API Motorline.
    Em caso de 401, renova o token e tenta uma vez.
    """
    global _token, _token_expires_at
    opts = load_options()
    base = opts.get("api_base_url", "https://api.mconnect.motorline.pt").rstrip("/")
    url = f"{base}/devices/value/{device_id}"

    for attempt in range(2):
        token, error_msg = ensure_token()
        if not token:
            return False, error_msg or "Falha ao obter token"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            # API MConnect exige POST (PUT devolve 405 Method Not Allowed)
            r = requests.post(
                url,
                json={"value": value},
                headers=headers,
                timeout=15,
            )
            if r.status_code == 401:
                logger.warning("Token expirado (401), a renovar...")
                _token, _token_expires_at = None, 0.0
                continue
            if r.status_code in (200, 204):
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            return False, str(e)

    return False, "401 após renovação de token"


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
          setPanelClass('success');
          setPanel('<p><strong>Operacional</strong></p>' +
            (d.device_id ? '<p>Dispositivo: <code>' + (d.device_id.length > 20 ? d.device_id.slice(0,12)+'…' : d.device_id) + '</code></p>' : ''));
          return;
        }
        if (d.status === 'awaiting_code') {
          setPanelClass(d.token_expired_alert ? 'alert' : '');
          var title = d.token_expired_alert
            ? '<p><strong>Sessão expirada.</strong> Foi enviado um novo código ao seu email. Introduza-o abaixo.</p>'
            : '<p>Foi enviado um código ao seu email. Introduza-o abaixo.</p>';
          setPanel(title +
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
        setPanelClass('');
        setPanel('<p>' + (d.message || 'Não autenticado') + '</p>' +
          '<button type="button" id="btnStart">Tentar novamente</button>');
        el('btnStart').onclick = startLogin;
      }).catch(function() { setPanel('<p>Erro a obter estado. A recarregar...</p>'); });
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
      }).then(function() { showMsg(''); poll(); }).catch(function() { showMsg('Erro de rede.', true); });
    }
    function startLogin() {
      var btn = el('btnStart');
      if (btn) btn.disabled = true;
      fetch('/login/start', { method: 'POST' }).then(function() { poll(); }).finally(function() { if (btn) btn.disabled = false; });
    }
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
        else { showMsg(res.error || 'Código inválido.', true); }
      }).catch(function() { showMsg('Erro de rede.', true); });
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
    """Inicia o fluxo de login. Body opcional: {"email": "...", "password": "..."} para guardar e fazer login."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if email and password:
        save_state({"email": email, "password": password})
    with _lock:
        ensure_token()
    return login_status()


@app.route("/login/verify", methods=["POST"])
def login_verify():
    """
    Submete o código recebido por email para completar o login.
    Body JSON: {"code": "123456"} ou query ?code=123456
    Se não houver device_id configurado, obtém a lista de dispositivos e guarda o primeiro.
    """
    global _token, _token_expires_at, _token_expired_alert
    code = None
    if request.is_json and request.json:
        code = (request.json.get("code") or request.json.get("otp") or "").strip()
    if not code:
        code = (request.args.get("code") or request.args.get("otp") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "Envie o código no body: {\"code\": \"123456\"}"}), 400

    token, expires_in = verify_code(code)
    if not token:
        return jsonify({"ok": False, "error": "Código inválido ou API não respondeu com token"}), 400

    _token = token
    _token_expires_at = time.time() + expires_in
    _token_expired_alert = False

    opts = load_options()
    device_id = (opts.get("device_id") or "").strip()
    if not device_id:
        devices = get_devices(token)
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
    port = int(os.environ.get("PORT", 8765))
    logger.info("A iniciar servidor na porta %s", port)
    t = threading.Thread(target=_background_tasks, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
