"""Politique d'accès : qui peut lire et écrire quels chemins du vault.

Deux rôles : `curator` (tout le vault, contenu et structure) et `contributor`
(motifs `read` / `write` explicites, pas de changement de structure).

Chaque client est décrit dans policy.yaml. Un client s'authentifie soit par
OAuth GitHub (liste d'identifiants numériques), soit par jeton statique, dont
seule l'empreinte SHA-256 est stockée.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_ALLOWED_KEYS = {"description", "auth", "github_ids", "token_sha256", "read", "write", "role"}


class PolicyError(ValueError):
    pass


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Convertit un motif de chemin en regex.

    `**` couvre n'importe quelle profondeur, `*` un segment, `?` un caractère.
    """
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(patterns: tuple[str, ...], path: str) -> bool:
    return any(_glob_regex(p).match(path) for p in patterns)


@dataclass(frozen=True)
class Client:
    name: str
    auth: str
    read: tuple[str, ...]
    write: tuple[str, ...]
    description: str = ""
    token_sha256: str | None = None
    github_ids: frozenset[str] = frozenset()
    role: str = "contributor"

    @property
    def is_curator(self) -> bool:
        return self.role == "curator"

    def can_read(self, path: str) -> bool:
        return matches(self.read, path) or matches(self.write, path)

    def can_write(self, path: str) -> bool:
        return matches(self.write, path)

    def summary(self) -> dict:
        return {
            "client": self.name,
            "auth": self.auth,
            "role": self.role,
            "read": list(self.read),
            "write": list(self.write),
        }


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Policy:
    clients: dict[str, Client]

    @classmethod
    def load(cls, path: Path) -> "Policy":
        data = yaml.safe_load(path.read_text()) or {}
        raw_clients = data.get("clients")
        if not isinstance(raw_clients, dict) or not raw_clients:
            raise PolicyError("policy.yaml doit définir au moins un client sous 'clients'")
        clients = {name: _parse_client(name, spec) for name, spec in raw_clients.items()}
        hashes = [c.token_sha256 for c in clients.values() if c.token_sha256]
        if len(hashes) != len(set(hashes)):
            raise PolicyError("deux clients partagent la même empreinte de jeton")
        return cls(clients=clients)

    def by_token(self, token: str) -> Client | None:
        digest = hash_token(token)
        found = None
        for client in self.clients.values():
            if client.token_sha256 and hmac.compare_digest(client.token_sha256, digest):
                found = client
        return found

    def by_github_id(self, github_id: str) -> Client | None:
        for client in self.clients.values():
            if client.auth == "github" and github_id in client.github_ids:
                return client
        return None

    @property
    def github_ids(self) -> set[str]:
        return {i for c in self.clients.values() if c.auth == "github" for i in c.github_ids}


def _parse_client(name: str, spec: object) -> Client:
    if not _NAME.match(str(name)):
        raise PolicyError(f"nom de client invalide : {name!r}")
    if not isinstance(spec, dict):
        raise PolicyError(f"client {name} : la définition doit être un dictionnaire")
    unknown = set(spec) - _ALLOWED_KEYS
    if unknown:
        raise PolicyError(f"client {name} : clés inconnues {sorted(unknown)}")

    auth = spec.get("auth")
    role = spec.get("role", "contributor")
    if role not in ("curator", "contributor"):
        raise PolicyError(f"client {name} : 'role' doit valoir 'curator' ou 'contributor'")
    read = tuple(spec.get("read") or ())
    write = tuple(spec.get("write") or ())
    if role == "curator":
        if read or write:
            raise PolicyError(f"client {name} : un curateur a tous les droits, 'read' et 'write' sont interdits")
        read = write = ("**",)
    for pattern in read + write:
        if not isinstance(pattern, str) or pattern.startswith("/") or ".." in pattern:
            raise PolicyError(f"client {name} : motif invalide {pattern!r}")

    if auth == "github":
        ids = spec.get("github_ids") or []
        if not ids:
            raise PolicyError(f"client {name} : 'github_ids' est requis avec auth: github")
        return Client(name, auth, read, write, spec.get("description", ""),
                      github_ids=frozenset(str(i) for i in ids), role=role)
    if auth == "token":
        digest = str(spec.get("token_sha256") or "").lower()
        if not _HEX64.match(digest):
            raise PolicyError(f"client {name} : 'token_sha256' doit être une empreinte SHA-256")
        return Client(name, auth, read, write, spec.get("description", ""), token_sha256=digest, role=role)
    raise PolicyError(f"client {name} : 'auth' doit valoir 'github' ou 'token'")
