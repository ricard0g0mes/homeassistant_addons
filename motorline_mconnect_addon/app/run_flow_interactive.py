#!/usr/bin/env python3
"""
Fluxo interativo: API real, para no pedido do código por email e pede-o no terminal.
Uso: cd motorline_mconnect_addon/app && python run_flow_interactive.py
Requer motorline_state.json com "email" e "password" (ou variáveis de ambiente).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Importar depois do path para usar main com DATA_DIR = app/
import main


def run():
    opts = main.load_options()
    email = opts.get("email", "").strip() or os.environ.get("MOTORLINE_EMAIL", "").strip()
    password = opts.get("password") or os.environ.get("MOTORLINE_PASSWORD", "")

    if not email:
        email = input("Email: ").strip()
    if not password:
        import getpass
        password = getpass.getpass("Password: ")

    if email and password:
        main.save_state({"email": email, "password": password})

    print("A pedir código por email (login na API)...")
    token, _ = main.login(
        opts.get("api_base_url", main.API_BASE_URL).rstrip("/"),
        email,
        password,
    )

    if token:
        print("Login obteve token direto (sem MFA).")
    elif main._awaiting_code:
        print("\n>>> Abra o email e introduza o código abaixo. <<<\n")
        code = input("Código do email: ").strip()
        if not code:
            print("Sem código. A sair.")
            return 1
        token, exp = main.verify_code(code)
        if not token:
            print("Código inválido ou expirado.")
            return 1
        print("Código aceite. Token obtido.")
    else:
        print("Login falhou ou estado inesperado.")
        return 1

    device_id = (opts.get("device_id") or "").strip()
    if not device_id:
        device_id = input("Device ID (ou Enter para saltar trigger): ").strip()
    if device_id:
        ok, err = main.set_device_value(device_id, 2)
        print("Trigger:", "OK" if ok else f"Erro: {err}")
    else:
        print("Sem device_id. Trigger ignorado.")

    return 0


if __name__ == "__main__":
    sys.exit(run())
