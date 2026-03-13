#!/usr/bin/env python3
"""
Testes do addon Motorline MConnect.
O fluxo real para no estado "à espera do código por email"; o utilizador introduz
o código no painel. Estes testes mockam a API para validar a lógica até esse
ponto (login → awaiting_code) e, em separado, as funções verify/set_value.
Correr: cd motorline_mconnect_addon/app && python -m unittest test_motorline -v
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Garantir que "import main" resolve para app/main.py
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# DATA_DIR para testes: não usar /data
TEST_DATA = APP_DIR / "test_data"
TEST_DATA.mkdir(exist_ok=True)
STATE_PATH = TEST_DATA / "motorline_state.json"


def _reset_state():
    if STATE_PATH.exists():
        STATE_PATH.unlink()


@patch("main.DATA_DIR", TEST_DATA)
@patch("main.STATE_PATH", STATE_PATH)
@patch("main._LOGIN_LOCK_PATH", TEST_DATA / ".motorline_login.lock")
class TestMotorlineFlow(unittest.TestCase):
    """Testes do fluxo login → verify → trigger com mocks da API."""

    def setUp(self):
        _reset_state()
        state = {
            "email": "test@example.com",
            "password": "testpass",
            "device_id": "66755146c8a511e8645bd710",
        }
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def tearDown(self):
        _reset_state()

    @patch("main.requests.post")
    @patch("main.requests.get")
    def test_login_sets_awaiting_code_when_mfa_required(self, mock_get, mock_post):
        import main

        main._awaiting_code = False
        main._login_session = None
        mock_post.return_value = MagicMock(
            status_code=200,
            text=json.dumps({
                "mfa_required": True,
                "access_token": "mfa_token_xyz",
                "expires_in": 3600,
            }),
        )
        token, exp = main.login(
            main.API_BASE_URL.rstrip("/"),
            "test@example.com",
            "testpass",
        )
        self.assertTrue(main._awaiting_code)
        self.assertIsNone(token)
        self.assertEqual(exp, 0)
        mock_post.assert_called()
        call_url = mock_post.call_args[0][0]
        self.assertIn("auth/token", call_url)

    @patch("main.requests.post")
    def test_verify_code_returns_token(self, mock_post):
        import main

        main._awaiting_code = True
        main._login_session = {
            "api_base_url": main.API_BASE_URL.rstrip("/"),
            "email": "test@example.com",
        }
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"access_token": "user_jwt_abc", "expires_in": 3600}
        resp.text = "{}"
        mock_post.return_value = resp
        token, exp = main.verify_code("123456")
        self.assertEqual(token, "user_jwt_abc")
        self.assertEqual(exp, 3600)

    @patch("main.requests.get")
    @patch("main.requests.post")
    def test_exchange_user_token_for_home_token(self, mock_post, mock_get):
        import main

        get_resp = MagicMock(status_code=200)
        get_resp.json.return_value = [{"_id": "home123"}]
        mock_get.return_value = get_resp
        post_resp = MagicMock(status_code=200)
        post_resp.json.return_value = {"access_token": "home_token_xyz", "expires_in": 3600}
        mock_post.return_value = post_resp
        token, exp, home_id = main.exchange_user_token_for_home_token("user_jwt")
        self.assertEqual(token, "home_token_xyz")
        self.assertEqual(exp, 3600)
        self.assertEqual(home_id, "home123")

    @patch("main.requests.post")
    def test_post_device_value_success(self, mock_post):
        import main

        mock_post.return_value = MagicMock(status_code=200, text="")
        ok, status, err = main._post_device_value(
            "66755146c8a511e8645bd710", 2, "any_token"
        )
        self.assertTrue(ok)
        self.assertEqual(status, 200)
        self.assertEqual(err, "")
        call_args = mock_post.call_args
        self.assertIn("devices/value", call_args[0][0])
        self.assertEqual(call_args[1]["json"].get("value_id"), "gate_state")
        self.assertEqual(call_args[1]["json"].get("value"), 2)

    @patch("main.requests.post")
    @patch("main.ensure_token")
    def test_set_device_value_success_with_user_token(self, mock_ensure, mock_post):
        import main

        mock_ensure.return_value = ("home_tok", "")
        mock_post.return_value = MagicMock(status_code=200, text="")
        ok, err = main.set_device_value("66755146c8a511e8645bd710", 2)
        self.assertTrue(ok)
        self.assertEqual(err, "")


@patch("main.STATE_PATH", STATE_PATH)
class TestTriggerEndpoint(unittest.TestCase):
    def setUp(self):
        _reset_state()

    def test_trigger_returns_400_without_device_id(self):
        """Sem device_id configurado, /trigger deve devolver 400."""
        import main

        with main.app.test_client() as c:
            r = c.post("/trigger")
            self.assertEqual(r.status_code, 400)
            data = r.get_json()
            self.assertFalse(data.get("ok"))
            self.assertIn("device_id", (data.get("error") or "").lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
