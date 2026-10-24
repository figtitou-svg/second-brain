"""Refait l'autorisation Google sur ton PC (ouvre le navigateur) avec TOUS les droits de GOOGLE_SCOPES.

    python -m scripts.reauth_google

À lancer sur le PC (la VM n'a pas de navigateur), puis copier secrets/token.json sur la VM.
Écrit le jeton uniquement après une autorisation réussie ; l'ancien est gardé en secrets/token.json.bak.
"""
from __future__ import annotations

import os
import shutil

from dotenv import load_dotenv

load_dotenv()

from coach.calendar_client import DEFAULT_SCOPES  # noqa: E402


def main() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    token = "./secrets/token.json"
    scopes = os.environ.get("GOOGLE_SCOPES", DEFAULT_SCOPES).split(",")
    flow = InstalledAppFlow.from_client_secrets_file(
        os.environ.get("GOOGLE_CLIENT_SECRET_FILE", "./secrets/client_secret.json"), scopes)
    creds = flow.run_local_server(port=0)
    if os.path.exists(token):
        shutil.copy(token, token + ".bak")
    with open(token, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print("Droits accordés :", ", ".join(s.rsplit("/", 1)[-1] for s in (creds.scopes or [])))


if __name__ == "__main__":
    main()
