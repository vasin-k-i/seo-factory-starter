"""
Одноразовый скрипт получения OAuth refresh_token для GSC.

Запускается ОДИН раз локально (с GUI/браузером), сохраняет refresh_token в .env.
Потом этот token живёт долго (год+) и используется в gsc_client.py.

Зачем нужен этот скрипт:
    GSC service-account email иногда не принимает в Users and permissions.
    OAuth user credentials — обход этой проблемы: запрос идёт от имени реального
    Google-аккаунта (gmail-владельца GSC), который УЖЕ имеет доступ.

Использование:
    cd seo-agent && python3 modules/gsc_oauth_setup.py

ENV (вход):
    GSC_OAUTH_CLIENT_FILE — путь к client_secret_...json от OAuth Client ID.

ENV (выход — допишется в .env):
    GSC_OAUTH_REFRESH_TOKEN
    GSC_OAUTH_CLIENT_ID
    GSC_OAUTH_CLIENT_SECRET
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/webmasters"]
# Full write scope: позволяет читать данные GSC (как readonly), плюс делать
# sitemaps.submit() и sitemaps.delete(). Используется для программного
# переобхода после релизов (M1+M2 read остаются совместимыми).


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_file = os.environ.get("GSC_OAUTH_CLIENT_FILE", "").strip()
    if not client_file:
        print("⚠ ENV GSC_OAUTH_CLIENT_FILE не задан.")
        print("  Положи скачанный из Google Cloud Console JSON в seo-agent/secrets/")
        print("  и пропиши путь: GSC_OAUTH_CLIENT_FILE=secrets/gsc-oauth-client.json")
        sys.exit(1)

    p = Path(client_file)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[1] / p
    if not p.exists():
        print(f"⚠ Файл не найден: {p}")
        sys.exit(1)

    print(f"→ Запускаю OAuth-флоу с {p}")
    print("→ Сейчас откроется браузер. Войди в Google-аккаунт, который владеет GSC,")
    print("  и нажми «Allow» на странице согласия. Если страница покажет «App not")
    print("  verified» — нажми «Advanced → Go to seo-agent (unsafe)» (это нормально")
    print("  для своего же приложения в режиме Testing).")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(p), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    print("\n✓ Авторизация прошла.")
    print(f"  refresh_token получен (длина {len(creds.refresh_token or '')} символов)")

    # Сохраняем в .env (рядом с TELEGRAM_*)
    env_path = Path(__file__).resolve().parents[1] / ".env"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    # Удалим старые ключи если были
    keys_to_remove = {"GSC_OAUTH_REFRESH_TOKEN", "GSC_OAUTH_CLIENT_ID", "GSC_OAUTH_CLIENT_SECRET"}
    lines = [ln for ln in lines if not any(ln.startswith(k + "=") for k in keys_to_remove)]

    # Читаем client_id/secret из исходного JSON
    with open(p) as f:
        client_data = json.load(f)
    if "installed" in client_data:
        client_data = client_data["installed"]
    elif "web" in client_data:
        client_data = client_data["web"]

    lines.append("")
    lines.append("# GSC OAuth (получено через gsc_oauth_setup.py)")
    lines.append(f"GSC_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    lines.append(f"GSC_OAUTH_CLIENT_ID={client_data['client_id']}")
    lines.append(f"GSC_OAUTH_CLIENT_SECRET={client_data['client_secret']}")
    env_path.write_text("\n".join(lines) + "\n")
    print(f"✓ Записано в {env_path}")
    print("→ Теперь можно прогонять modules/gsc_client.py — должен видеть property и данные.")


if __name__ == "__main__":
    main()
