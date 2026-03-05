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


def load_options():
    with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def login(api_base_url: str, email: str, password: str) -> tuple[str | None, int]:
    """
    Faz login na API Motorline. Retorna (access_token, expires_in_seconds) ou (None, 0).
    Tenta endpoints comuns: /auth/login, /login.
    """
    for endpoint in ("/auth/login", "/login", "/api/auth/login", "/api/login"):
        url = f"{api_base_url.rstrip('/')}{endpoint}"
        try:
            r = requests.post(
                url,
                json={"email": email, "password": password},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            token = data.get("access_token") or data.get("token") or data.get("accessToken")
            if not token:
                continue
            # Algumas APIs devolvem expires_in em segundos
            expires_in = int(data.get("expires_in", data.get("expiresIn", 3600)))
            logger.info("Login OK via %s, token expira em %s s", endpoint, expires_in)
            return token, expires_in
        except Exception as e:
            logger.debug("Tentativa %s falhou: %s", endpoint, e)
            continue

    # Tentativa form-urlencoded
    for endpoint in ("/auth/login", "/login"):
        url = f"{api_base_url.rstrip('/')}{endpoint}"
        try:
            r = requests.post(
                url,
                data={"email": email, "password": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            token = data.get("access_token") or data.get("token") or data.get("accessToken")
            if not token:
                continue
            expires_in = int(data.get("expires_in", data.get("expiresIn", 3600)))
            logger.info("Login OK (form) via %s", endpoint)
            return token, expires_in
        except Exception as e:
            logger.debug("Tentativa form %s falhou: %s", endpoint, e)

    logger.error("Login falhou em todos os endpoints tentados")
    return None, 0


def ensure_token() -> str | None:
    """Obtém um token válido (renovando se necessário)."""
    global _token, _token_expires_at
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


@app.route("/")
def index():
    """Página inicial com informações sobre o addon."""
    return jsonify({
        "name": "Motorline MConnect Proxy",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "/health": "GET - Verifica se o serviço está a funcionar",
            "/trigger": "GET/POST - Dispara o comando do portão (query: ?value=1)",
            "/command": "GET/POST - Alias para /trigger",
            "/device/value": "PUT/POST - Define valor do dispositivo (body: {\"value\": 1})"
        }
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


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
