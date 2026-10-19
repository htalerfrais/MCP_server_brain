"""Génère un jeton statique pour un nouveau client.

Le jeton n'est affiché qu'une fois. Seule son empreinte va dans policy.yaml.
"""

from __future__ import annotations

import argparse
import secrets

from .policy import hash_token


def main() -> None:
    parser = argparse.ArgumentParser(description="Crée un jeton pour un client du serveur MCP.")
    parser.add_argument("client", help="nom du client dans policy.yaml, par exemple hermes")
    args = parser.parse_args()

    token = "brain_" + secrets.token_urlsafe(32)
    print(f"Jeton pour {args.client} (à transmettre au client, non conservé ici) :")
    print(f"  {token}")
    print()
    print("Entrée à ajouter dans policy.yaml :")
    print(f"  {args.client}:")
    print("    auth: token")
    print(f"    token_sha256: {hash_token(token)}")
    print('    read: ["**"]')
    print("    write: []")


if __name__ == "__main__":
    main()
