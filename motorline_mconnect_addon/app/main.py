#!/usr/bin/env python3
from __future__ import annotations

"""
Addon Home Assistant: proxy para API Motorline MConnect.
Renova o token automaticamente em 401 e expõe endpoint local para comando do portão.
"""
import json
import logging
import os
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

# Configuração (HA injecta em /data/options.json)
OPTIONS_PATH = Path("/data/options.json")
if not OPTIONS_PATH.exists():
    OPTIONS_PATH = Path(__file__).parent / "options.json"

app = Flask(__name__)

# Estado do token (em memória; renovado ao receber 401 ou antes do expiry)
_token = None
_token_expires_at = 0.0

# Login em 2 fases: quando a API envia código por email
_awaiting_code = False
_login_session = None  # {"api_base_url", "email", "session_id" ou "request_id" (se a API devolver)}


def load_options():
    with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


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


def ensure_token() -> str | None:
    """Obtém um token válido (renovando se necessário). Se estiver à espera de código, retorna None."""
    global _token, _token_expires_at
    if _awaiting_code:
        return None
    opts = load_options()
    refresh_before = int(opts.get("refresh_before_expiry_seconds", 300))
    now = time.time()

    if _token and _token_expires_at > now + refresh_before:
        return _token

    api_base_url = opts.get("api_base_url", "https://api.mconnect.motorline.pt")
    email = opts.get("email", "")
    password = opts.get("password", "")
    if not email or not password:
        logger.error("email ou password em falta nas opções")
        return None

    token, expires_in = login(api_base_url, email, password)
    if not token:
        return None

    _token = token
    _token_expires_at = now + expires_in
    return _token


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
        token = ensure_token()
        if not token:
            if _awaiting_code:
                return False, "À espera do código por email. Submeta em POST /login/verify com {\"code\": \"...\"}"
            return False, "Falha ao obter token"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            # Assumir PUT ou PATCH com body {"value": ...}; se a API for diferente, ajustar
            r = requests.put(
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


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/login/status", methods=["GET"])
def login_status():
    """Indica se o add-on está à espera do código de verificação por email."""
    global _awaiting_code, _token
    if _awaiting_code:
        return jsonify({"status": "awaiting_code", "message": "Abra o email e submeta o código em POST /login/verify"})
    if _token:
        return jsonify({"status": "ready", "message": "Sessão ativa"})
    return jsonify({"status": "not_logged_in", "message": "Faça login (ou aguarde renovação)"})


@app.route("/login/verify", methods=["POST"])
def login_verify():
    """
    Submete o código recebido por email para completar o login.
    Body JSON: {"code": "123456"} ou query ?code=123456
    """
    global _token, _token_expires_at
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
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
