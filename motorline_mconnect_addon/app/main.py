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

# Modo de teste: False = usar login + código por email e token da casa (recomendado).
NO_AUTH_MODE = False

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

# Opções do addon (HA injeta em /data/options.json)
ADDON_OPTIONS_PATH = DATA_DIR / "options.json"

# Sessão HTTP com cookies persistentes (igual à página Motorline no browser)
COOKIES_PATH = DATA_DIR / "motorline_cookies.json"
_session: requests.Session | None = None
_session_lock = threading.Lock()


def get_http_session() -> requests.Session:
    """Sessão única; cookies carregados de disco e enviados em todos os pedidos (como no browser)."""
    global _session
    with _session_lock:
        if _session is None:
            _session = requests.Session()
            _session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Accept-Language": "pt,en-US;q=0.9,en;q=0.8",
                "Origin": "https://mconnect.motorline.pt",
                "Content-Type": "application/json",
            })
            _load_session_cookies()
        return _session


def _load_session_cookies():
    """Carrega cookies guardados em disco para a sessão (sobrevive a reinícios do addon)."""
    if not COOKIES_PATH.exists():
        return
    try:
        with open(COOKIES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for c in data if isinstance(data, list) else []:
            if isinstance(c, dict) and c.get("name") and c.get("value") is not None:
                _session.cookies.set(
                    c["name"],
                    c["value"],
                    domain=c.get("domain") or "",
                    path=c.get("path") or "/",
                    expires=c.get("expires"),
                )
        logger.info("Cookies de sessão carregados (%s)", len(data) if isinstance(data, list) else 0)
    except Exception as e:
        logger.debug("load_session_cookies: %s", e)


def _save_session_cookies():
    """Guarda cookies da sessão em disco (igual à persistência no browser)."""
    if _session is None:
        return
    try:
        out = []
        for c in _session.cookies:
            out.append({
                "name": c.name,
                "value": c.value,
                "domain": getattr(c, "domain", "") or "",
                "path": getattr(c, "path", "") or "/",
                "expires": getattr(c, "expires", None),
            })
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(COOKIES_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=0)
    except Exception as e:
        logger.debug("save_session_cookies: %s", e)


def _open_motorline_page() -> None:
    """Abre a página da Motorline (GET) para obter os mesmos cookies que o browser recebe."""
    try:
        s = get_http_session()
        r = s.get("https://mconnect.motorline.pt/", timeout=15)
        if r.status_code == 200 and r.cookies:
            _save_session_cookies()
            logger.info("Página Motorline carregada; cookies recebidos e guardados.")
    except Exception as e:
        logger.debug("_open_motorline_page: %s", e)


def load_addon_options() -> dict:
    """Lê opções do addon (MQTT, etc.) de /data/options.json (injetado pelo HA)."""
    if not ADDON_OPTIONS_PATH.exists():
        return {}
    try:
        with open(ADDON_OPTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.debug("load_addon_options: %s", e)
        return {}


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
        "user_token": (state.get("user_token") or "").strip() or None,
        "user_token_expires_at": state.get("user_token_expires_at") or 0,
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
    session = get_http_session()
    try:
        r = session.post(url, json=body, headers=headers, timeout=15)
        if r.cookies:
            _save_session_cookies()
        data = r.json() if r.text else {}
        token = data.get("access_token") or data.get("token") or data.get("accessToken")
        if r.status_code == 200 and token:
            if data.get("mfa_required") or data.get("requires_verification") or data.get("mfa") is True:
                _awaiting_code = True
                _login_session = {"api_base_url": base, "email": email, "access_token": token}
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
            _login_session = {"api_base_url": base, "email": email, "access_token": token}
            for key in ("session_id", "request_id", "mfa_token", "mfa_request_id", "state", "nonce", "session"):
                if data.get(key) is not None:
                    _login_session[key] = data[key]
            return None, 0
        if r.status_code == 401 or _is_mfa_required_response(r.status_code, data):
            _awaiting_code = True
            _login_session = {"api_base_url": base, "email": email}
            if token:
                _login_session["access_token"] = token
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
    # Body igual ao HAR: code, platform, model, uuid (browser não envia email nem otp)
    payload = {
        "code": code_clean,
        "platform": "Linux",
        "model": "addon",
        "uuid": MFA_DEVICE_UUID,
    }
    for key in ("session_id", "request_id", "mfa_token", "mfa_request_id", "state", "nonce", "session"):
        if _login_session.get(key) is not None:
            payload[key] = _login_session[key]

    session = get_http_session()

    def try_verify(url: str, body: dict, headers: dict | None = None) -> tuple[str | None, int]:
        if headers is None:
            headers = {"Content-Type": "application/json"}
        try:
            r = session.post(url, json=body, headers=headers, timeout=15)
            if r.cookies:
                _save_session_cookies()
            data = r.json() if r.text else {}
            token = data.get("access_token") or data.get("token") or data.get("accessToken")
            if r.status_code == 200 and token:
                exp = int(data.get("expires_in", data.get("expiresIn", 3600)))
                return token, exp
            logger.warning("MFA verify %s: status=%s body=%s", url, r.status_code, data)
        except Exception as e:
            logger.debug("MFA verify %s: %s", url, e)
        return None, 0

    # /user/mfa/verify exige Authorization: Bearer <token do primeiro login>
    mfa_headers = {"Content-Type": "application/json"}
    if _login_session.get("access_token"):
        mfa_headers["Authorization"] = f"Bearer {_login_session['access_token']}"
    for url in (f"{base}/user/mfa/verify", f"{base}/auth/mfa/verify", f"{base}/mfa/verify"):
        token, exp = try_verify(url, payload, mfa_headers)
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
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Content-Type": "application/json",
        "Origin": "https://mconnect.motorline.pt",
        "timezone": "Europe/Lisbon",
    }
    session = get_http_session()
    try:
        r = session.get(f"{base}/homes", headers=headers, timeout=15)
        if r.cookies:
            _save_session_cookies()
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
        r2 = session.post(
            f"{base}/homes/auth/token",
            json={"grant_type": "authorization", "code": user_token, "home_id": home_id},
            headers={
                "Content-Type": "application/json",
                "Origin": "https://mconnect.motorline.pt",
                "timezone": "Europe/Lisbon",
            },
            timeout=15,
        )
        if r2.cookies:
            _save_session_cookies()
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


def exchange_for_device_token(user_token: str, home_token: str | None, home_id: str | None, device_id: str) -> tuple[str | None, int]:
    """
    Tenta obter um token com device_id no payload (api.mconnect.motorline.pt exige isso).
    Prova rest.mconnect.pt e depois api.mconnect.motorline.pt.
    """
    # 1) rest.mconnect.pt
    base = API_BASE_URL.rstrip("/")
    for code, label in [(user_token, "user"), (home_token or user_token, "home")]:
        if not code:
            continue
        session = get_http_session()
        for path, body in [
            (f"{base}/devices/auth/token", {"grant_type": "authorization", "code": code, "device_id": device_id}),
            (f"{base}/devices/auth/token", {"grant_type": "authorization", "code": code, "device_id": device_id, "home_id": home_id or ""}),
            (f"{base}/homes/devices/auth/token", {"grant_type": "authorization", "code": code, "device_id": device_id, "home_id": home_id or ""}),
        ]:
            try:
                r = session.post(path, json=body, headers={"Content-Type": "application/json"}, timeout=10)
                data = r.json() if r.text else {}
                tok = data.get("access_token") or data.get("token") or data.get("accessToken")
                if r.status_code == 200 and tok:
                    exp = int(data.get("expires_in", data.get("expiresIn", 3600)))
                    logger.info("Token por dispositivo obtido em %s (exp %s s)", path, exp)
                    return tok, exp
            except Exception as e:
                logger.debug("exchange_for_device_token %s: %s", path, e)

    # 2) api.mconnect.motorline.pt (API do portão pode emitir o seu próprio token)
    dev_base = DEVICES_API_BASE_URL.rstrip("/")
    for code in (user_token, home_token):
        if not code:
            continue
        for path, body in [
            (f"{dev_base}/auth/token", {"grant_type": "authorization", "code": code, "device_id": device_id}),
            (f"{dev_base}/devices/auth/token", {"grant_type": "authorization", "code": code, "device_id": device_id}),
            (f"{dev_base}/auth/token", {"grant_type": "authorization", "access_token": code, "device_id": device_id}),
        ]:
            try:
                r = session.post(path, json=body, headers={"Content-Type": "application/json"}, timeout=10)
                data = r.json() if r.text else {}
                tok = data.get("access_token") or data.get("token") or data.get("accessToken")
                if r.status_code == 200 and tok:
                    exp = int(data.get("expires_in", data.get("expiresIn", 3600)))
                    logger.info("Token devices API obtido em %s (exp %s s)", path, exp)
                    return tok, exp
            except Exception as e:
                logger.debug("exchange_for_device_token %s: %s", path, e)
    return None, 0


def _get_rooms(token: str) -> list[dict]:
    """GET /rooms na rest.mconnect.pt. Usa sessão com cookies (como o browser) e opcionalmente Bearer."""
    base = API_BASE_URL.rstrip("/")
    headers = {"Content-Type": "application/json", "timezone": "Europe/Lisbon"}
    if not NO_AUTH_MODE and token:
        headers["Authorization"] = f"Bearer {token}"
    session = get_http_session()
    try:
        r = session.get(f"{base}/rooms", headers=headers, timeout=15)
        if r.cookies:
            _save_session_cookies()
        if r.status_code == 200 and r.text:
            raw = r.json()
            return raw if isinstance(raw, list) else []
        if r.status_code != 200:
            logger.warning("GET /rooms devolveu %s: %s", r.status_code, r.text[:200] if r.text else "")
    except Exception as e:
        logger.debug("_get_rooms: %s", e)
    return []


# Valores reais da API para o portão: 0=fechado, 2=aberto, 6=fechando, 8=abrindo
GATE_STATE_MAP = {0: "fechado", 2: "aberto", 6: "fechando", 8: "abrindo"}


def gate_value_to_state(raw_value: int | float) -> str:
    """Converte o valor bruto da API (0, 2, 6, 8) no estado do portão."""
    v = int(raw_value) if raw_value is not None else 0
    return GATE_STATE_MAP.get(v, "desconhecido")


def get_gate_state(device_id: str, token: str) -> dict | None:
    """
    Estado do portão. GET /rooms → devices[].values[] com value_id "gate_state".
    Retorna {"value": raw (0/2/6/8), "state": "fechado"|"aberto"|"fechando"|"abrindo", "unit": "%"}.
    """
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
                    return {
                        "value": raw,
                        "state": gate_value_to_state(raw),
                        "unit": v.get("unit", "%"),
                    }
    return None


def get_devices(token: str) -> list[dict]:
    """Lista dispositivos. Na rest.mconnect.pt vêm em GET /rooms (cada room tem devices)."""
    rooms = _get_rooms(token)
    out = []
    for room in rooms:
        for d in room.get("devices", []):
            if isinstance(d, dict):
                out.append({"id": d.get("_id", d.get("id", d.get("device_id", ""))), "name": d.get("name", d.get("label", ""))})
    return out


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
    """Retorna o token se válido (memória ou state).

    Em NO_AUTH_MODE devolve sempre um token "dummy" e não faz login nem validações.
    """
    if NO_AUTH_MODE:
        return "noauth", ""
    global _token, _token_expires_at, _user_token, _user_token_expires_at
    if _awaiting_code:
        return None, "À espera do código por email"
    opts = load_options()
    refresh_before = int(opts.get("refresh_before_expiry_seconds", REFRESH_BEFORE_EXPIRY_SECONDS))
    now = time.time()
    # Hidratar user_token do state se em memória estiver vazio
    if not _user_token and opts.get("user_token") and (opts.get("user_token_expires_at") or 0) > now:
        _user_token = opts["user_token"]
        _user_token_expires_at = float(opts["user_token_expires_at"])
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
    if NO_AUTH_MODE:
        logger.info("NO_AUTH_MODE ativo: verificação de token desativada.")
        return
    time.sleep(2)
    logger.info("Verificação de token em background ativa (intervalo: 1 h); o código só é pedido ao clicar no botão no painel.")
    while True:
        time.sleep(3600)
        now = time.time()
        if (not _token or _token_expires_at <= now) and not _awaiting_code:
            _token_expired_alert = True
            logger.warning("Token expirado. Abra o painel e clique em 'Pedir código por email' para obter um novo código.")


# Tópicos MQTT para o HA descobrir sensor e botão
MQTT_TOPIC_STATE = "motorline/portao/state"
MQTT_TOPIC_COMMAND = "motorline/portao/command"
MQTT_DISCOVERY_PREFIX = "homeassistant"


def _mqtt_publish_state(client: "mqtt.Client") -> None:
    """Publica estado do portão no tópico state (para o sensor no HA)."""
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
    payload = json.dumps({"state": gate.get("state", "desconhecido"), "value": gate.get("value", 0)})
    client.publish(MQTT_TOPIC_STATE, payload, retain=True)


def _mqtt_thread() -> None:
    """Conecta ao broker, publica discovery (sensor + botão), subscreve comando e publica estado periodicamente."""
    if not mqtt:
        return
    addon = load_addon_options()
    if not addon.get("mqtt_enabled"):
        return
    host = str(addon.get("mqtt_host", "core-mosquitto")).strip()
    port = int(addon.get("mqtt_port", 1883))
    user = (addon.get("mqtt_user") or "").strip()
    password = (addon.get("mqtt_password") or "").strip()
    if not host:
        return
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
        client_id="motorline_mconnect_addon",
    )
    if user:
        client.username_pw_set(user, password or None)

    def on_connect(c, userdata, flags, reason_code):
        if reason_code != 0:
            logger.warning("MQTT conexão falhou: %s", reason_code)
            return
        logger.info("MQTT ligado ao broker %s:%s", host, port)
        # Discovery: sensor de estado
        device = {"identifiers": ["motorline_mconnect"], "name": "Motorline MConnect"}
        sensor_config = {
            "name": "Portão Motorline Estado",
            "state_topic": MQTT_TOPIC_STATE,
            "value_template": "{{ value_json.state }}",
            "unique_id": "motorline_portao_estado",
            "device": device,
        }
        c.publish(f"{MQTT_DISCOVERY_PREFIX}/sensor/motorline_portao_estado/config", json.dumps(sensor_config), retain=True)
        # Discovery: botão abrir portão
        button_config = {
            "name": "Portão Motorline Abrir",
            "command_topic": MQTT_TOPIC_COMMAND,
            "payload_press": "OPEN",
            "unique_id": "motorline_portao_abrir",
            "device": device,
        }
        c.publish(f"{MQTT_DISCOVERY_PREFIX}/button/motorline_portao_abrir/config", json.dumps(button_config), retain=True)
        # Discovery: botão fechar portão
        button_close_config = {
            "name": "Portão Motorline Fechar",
            "command_topic": MQTT_TOPIC_COMMAND,
            "payload_press": "CLOSE",
            "unique_id": "motorline_portao_fechar",
            "device": device,
        }
        c.publish(f"{MQTT_DISCOVERY_PREFIX}/button/motorline_portao_fechar/config", json.dumps(button_close_config), retain=True)
        c.subscribe(MQTT_TOPIC_COMMAND)

    def on_message(c, userdata, msg):
        opts = load_options()
        device_id = (opts.get("device_id") or "").strip()
        if not device_id:
            return
        try:
            payload = (msg.payload or b"").decode("utf-8").strip().upper()
        except Exception:
            payload = "OPEN"
        # CLOSE, 0, FECHAR → fechar (0); resto → abrir (2)
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
    time.sleep(2)
    logger.info("MQTT discovery publicado; sensor e botão devem aparecer no Home Assistant.")
    while True:
        time.sleep(30)
        try:
            _mqtt_publish_state(client)
        except Exception as e:
            logger.debug("MQTT publish state: %s", e)


def _post_device_value(device_id: str, num: int, token: str, body: dict | None = None) -> tuple[bool, int, str]:
    """
    POST /devices/value na rest.mconnect.pt (igual à app Motorline).
    Token da casa (Bearer) obtido via /homes/auth/token.
    """
    base = API_BASE_URL.rstrip("/")
    url = f"{base}/devices/value/{device_id}"
    payload = body or {"value_id": "gate_state", "value": num}
    # Em NO_AUTH_MODE não enviamos Authorization; replicar headers do HAR.
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://mconnect.motorline.pt",
        "timezone": "Europe/Lisbon",
        "Accept": "*/*",
    }
    if not NO_AUTH_MODE:
        headers["Authorization"] = f"Bearer {token}"
    logger.info("POST %s (no_auth=%s) body=%s", url, NO_AUTH_MODE, payload)
    session = get_http_session()
    r = session.post(url, json=payload, headers=headers, timeout=15)
    if r.cookies:
        _save_session_cookies()
    if r.status_code in (200, 204):
        logger.info("devices/value OK status=%s", r.status_code)
        return True, r.status_code, ""
    logger.warning("devices/value falhou status=%s body=%s", r.status_code, r.text[:300] if r.text else "")
    return False, r.status_code, r.text[:300] if r.text else ""


def set_device_value(device_id: str, value: str | int | float) -> tuple[bool, str]:
    """
    Define o valor do dispositivo (portão). rest.mconnect.pt/devices/value (como a app).
    Usa token da casa (Bearer).
    """
    num = 2 if value in (1, "1", "open") else int(value) if isinstance(value, (int, float)) else 2

    token, error_msg = ensure_token()
    if not token:
        return False, error_msg or "Falha ao obter token"

    ok, status, err = _post_device_value(device_id, num, token)
    if ok:
        return True, ""

    if status == 401 and not NO_AUTH_MODE:
        global _token_expired_alert
        _token_expired_alert = True
        return False, "401 — token expirado. Tenta novamente pedir código por email ou verificar o login."

    return False, f"HTTP {status}: {err}"


@app.route("/api/ui-state", methods=["GET"])
def api_ui_state():
    """Estado para o painel: status, has_credentials, token_expired_alert, device_id, email, token_preview, gate_state."""
    opts = load_options()
    device_id = (opts.get("device_id") or "").strip()
    status_res = login_status().get_json()
    has_credentials = bool((opts.get("email") or "").strip() and opts.get("password"))
    token_str = (opts.get("token") or "").strip()
    out = {
        "status": status_res["status"],
        "message": status_res["message"],
        "token_expired_alert": status_res.get("token_expired_alert", False),
        "no_auth_mode": NO_AUTH_MODE,
        "device_id": device_id or None,
        "email": (opts.get("email") or "").strip() or None,
        "token_preview": (token_str[:32] + "…") if len(token_str) > 32 else (token_str or None),
        "has_credentials": has_credentials,
    }
    if device_id:
        token, _ = ensure_token()
        if token:
            gate = get_gate_state(device_id, token)
            if gate is not None:
                out["gate_state"] = gate.get("value", 0)
                out["gate_state_state"] = gate.get("state", "desconhecido")
                out["gate_state_unit"] = gate.get("unit", "%")
    return jsonify(out)


@app.route("/api/token", methods=["POST"])
def api_set_token():
    """Guarda o token de autenticação. Body: {\"token\": \"...\", \"expires_in\": 3600} (expires_in opcional)."""
    global _token, _token_expires_at
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "token em falta"}), 400
    expires_in = int(data.get("expires_in", 3600))
    expires_at = time.time() + expires_in
    save_state({"token": token, "token_expires_at": expires_at})
    _token = token
    _token_expires_at = expires_at
    return jsonify({"ok": True, "message": "Token guardado"})


@app.route("/api/gate-state", methods=["GET"])
def api_gate_state():
    """
    Sensor do portão: estado (fechado/aberto/fechando/abrindo) e valor bruto (0/2/6/8).
    Para HA: GET /api/gate-state → value (0,2,6,8), state (string).
    """
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
    return jsonify({
        "ok": True,
        "value": gate.get("value", 0),
        "state": gate.get("state", "desconhecido"),
        "unit": gate.get("unit", "%"),
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
  <div id="configCard" class="card" style="margin-top:1rem;">
    <p><strong>Configuração</strong></p>
    <p><label>Device ID:</label><br><input type="text" id="configDeviceId" placeholder="ID do dispositivo" style="margin-bottom:0.25rem;"><br><button type="button" id="btnSaveDeviceId" style="background:#6c757d;">Guardar device_id</button></p>
    <p><label>Email:</label><br><span id="configEmail" style="display:inline-block;margin:0.25rem 0;"></span></p>
    <p><label>Token (autenticação):</label><br><span id="configTokenPreview" style="display:inline-block;margin:0.25rem 0;word-break:break-all;font-size:0.85rem;"></span><br><input type="password" id="configTokenNew" placeholder="Colar novo token e guardar" style="margin-top:0.25rem;"><br><button type="button" id="btnSaveToken" style="background:#6c757d;margin-top:0.25rem;">Guardar token</button></p>
  </div>
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

    function setConfigCard(d) {
      el('configDeviceId').value = (d.device_id || '');
      el('configEmail').textContent = (d.email || '(não configurado)');
      el('configTokenPreview').textContent = (d.token_preview || '(nenhum)');
      if (!window.configCardInited) {
        window.configCardInited = true;
        el('btnSaveDeviceId').onclick = saveConfigDeviceId;
        el('btnSaveToken').onclick = saveConfigToken;
      }
    }
    function saveConfigDeviceId() {
      var id = (el('configDeviceId').value || '').trim();
      if (!id) { showMsg('Introduza o device_id.', true); return; }
      showMsg('A guardar...');
      fetch('/api/device_id', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ device_id: id }) })
        .then(r => r.json()).then(function(res) { showMsg(res.ok ? 'Device ID guardado.' : (res.error || 'Erro'), !res.ok); if (res.ok) poll(); })
        .catch(function() { showMsg('Erro de rede.', true); });
    }
    function saveConfigToken() {
      var tok = (el('configTokenNew').value || '').trim();
      if (!tok) { showMsg('Introduza o token.', true); return; }
      showMsg('A guardar...');
      fetch('/api/token', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: tok, expires_in: 3600 }) })
        .then(r => r.json()).then(function(res) { showMsg(res.ok ? 'Token guardado.' : (res.error || 'Erro'), !res.ok); if (res.ok) { el('configTokenNew').value = ''; poll(); } })
        .catch(function() { showMsg('Erro de rede.', true); });
    }

    function poll() {
      fetch('/api/ui-state').then(r => r.json()).then(function(d) {
        setConfigCard(d);
        if (d.status === 'ready') {
          if (!d.device_id && el('deviceIdInput')) return;
          setPanelClass('success');
          var html = '<p><strong>Operacional</strong></p>';
          if (d.device_id) {
            html += '<p>Dispositivo: <code>' + (d.device_id.length > 20 ? d.device_id.slice(0,12)+'…' : d.device_id) + '</code></p>';
            if (d.gate_state_state !== undefined) html += '<p>Estado do portão: <strong>' + (d.gate_state_state === 'fechado' ? 'Fechado' : d.gate_state_state === 'aberto' ? 'Aberto' : d.gate_state_state === 'fechando' ? 'Fechando' : d.gate_state_state === 'abrindo' ? 'Abrindo' : d.gate_state_state) + '</strong></p>';
            html += '<p><button type="button" id="btnTrigger">Abrir portão</button> <button type="button" id="btnClose" style="background:#6c757d;">Fechar portão</button></p>';
            if (d.token_expired_alert && !d.no_auth_mode) {
              html += '<p class="alert" style="margin:0.5rem 0 0 0; padding:0.5rem;">Sessão expirada. Use o botão abaixo para receber um novo código por email.</p>';
              html += '<p style="margin-top:0.5rem;"><button type="button" id="btnRenew" style="background:#6c757d;">Pedir novo código por email</button></p>';
            }
          } else {
            html += '<p class="alert" style="margin:0 0 0.75rem 0; padding:0.5rem;">ID do dispositivo não foi obtido. Introduza-o abaixo (ex: 66755146c8a511e8645bd710). Pode encontrá-lo na app Motorline ou no URL do dispositivo.</p>';
            html += '<input type="text" id="deviceIdInput" placeholder="ID do dispositivo (device_id)" style="margin-bottom:0.5rem;">';
            html += '<button type="button" id="btnSaveDevice">Guardar ID</button>';
          }
          setPanel(html);
          if (el('btnTrigger')) el('btnTrigger').onclick = triggerGate;
          if (el('btnClose')) el('btnClose').onclick = closeGate;
          if (el('btnRenew')) el('btnRenew').onclick = requestCode;
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
      fetch('/trigger', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"value":2}' }).then(r => r.json()).then(function(res) {
        showMsg(res.ok ? 'Comando abrir enviado.' : (res.error || 'Erro'), !res.ok);
      }).catch(function() { showMsg('Erro de rede.', true); }).finally(function() { if (btn) btn.disabled = false; });
    }
    function closeGate() {
      var btn = el('btnClose');
      if (btn) btn.disabled = true;
      fetch('/trigger', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"value":0}' }).then(r => r.json()).then(function(res) {
        showMsg(res.ok ? 'Comando fechar enviado.' : (res.error || 'Erro'), !res.ok);
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
    if NO_AUTH_MODE:
        return jsonify({"status": "ready", "message": "Sessão ativa (modo sem autenticação)", "token_expired_alert": False})
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
    if NO_AUTH_MODE:
        # Em modo sem auth ignoramos credenciais e marcamos logo como "ready".
        return jsonify({"status": "ready", "message": "Sessão ativa (modo sem autenticação)", "token_expired_alert": False}), 200
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
        _open_motorline_page()  # Cookies iguais ao browser ao abrir a página
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
    if NO_AUTH_MODE:
        return jsonify({"ok": True, "message": "Modo sem autenticação: não é necessário código."})
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
    _user_token = user_token
    _user_token_expires_at = time.time() + (home_expires if home_token else expires_in)
    _token_expired_alert = False

    device_id = (load_options().get("device_id") or "").strip()
    # O token que funciona em rest.mconnect.pt/devices/value é o da casa (home_token)
    if home_token:
        _token = home_token
        _token_expires_at = time.time() + home_expires
        logger.info("Token da casa guardado para comando do portão (expira em %s s)", home_expires)
        if home_id:
            save_state({"home_id": home_id})
    else:
        device_token, device_exp = exchange_for_device_token(user_token, home_token, home_id or "", device_id) if device_id else (None, 0)
        if device_token:
            _token = device_token
            _token_expires_at = time.time() + device_exp
        else:
            _token = user_token
            _token_expires_at = time.time() + expires_in
            logger.warning("Token da casa não obtido; a usar token de utilizador. Se o comando falhar, cole o token manualmente na configuração.")
    save_state({
        "token": _token,
        "token_expires_at": _token_expires_at,
        "user_token": _user_token,
        "user_token_expires_at": _user_token_expires_at,
    })

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
    get_http_session()  # Carrega cookies guardados (sessão como no browser)
    opts = load_options()
    now = time.time()
    if opts.get("token") and (opts.get("token_expires_at") or 0) > now:
        _token = opts["token"]
        _token_expires_at = float(opts["token_expires_at"])
        logger.info("Token restaurado do state")
    if opts.get("user_token") and (opts.get("user_token_expires_at") or 0) > now:
        _user_token = opts["user_token"]
        _user_token_expires_at = float(opts["user_token_expires_at"])
    port = int(os.environ.get("PORT", 8765))
    logger.info("A iniciar servidor na porta %s", port)
    t = threading.Thread(target=_background_tasks, daemon=True)
    t.start()
    t_mqtt = threading.Thread(target=_mqtt_thread, daemon=True)
    t_mqtt.start()
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
