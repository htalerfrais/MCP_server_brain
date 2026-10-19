"""Configuration lue depuis l'environnement et les credentials systemd."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    vault: Path
    policy_file: Path
    branch: str
    public_url: str | None
    public_host: str
    public_port: int
    internal_host: str
    internal_port: int
    allowed_hosts: list[str] = field(default_factory=list)
    github_client_id: str | None = None
    github_client_secret: str | None = None

    @property
    def public_enabled(self) -> bool:
        return bool(self.public_url and self.github_client_id and self.github_client_secret)


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _oauth_credentials() -> dict[str, str]:
    """Identifiants OAuth GitHub.

    En production, systemd les fournit via LoadCredential=github_oauth:... ;
    le fichier n'est alors lisible que par le service.
    """
    candidates = []
    if cred_dir := os.environ.get("CREDENTIALS_DIRECTORY"):
        candidates.append(Path(cred_dir) / "github_oauth")
    if explicit := os.environ.get("BRAIN_GITHUB_OAUTH_FILE"):
        candidates.append(Path(explicit))
    for path in candidates:
        if path.is_file():
            return _read_env_file(path)
    return {}


def load() -> Settings:
    env = os.environ
    public_url = env.get("BRAIN_PUBLIC_URL") or None
    creds = _oauth_credentials()

    allowed_hosts = [h for h in env.get("BRAIN_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if public_url:
        host = urlparse(public_url).hostname
        if host:
            allowed_hosts += [host, f"{host}:443", f"{host}:8443"]

    return Settings(
        vault=Path(env.get("BRAIN_VAULT", "/srv/brain/vault")),
        policy_file=Path(env.get("BRAIN_POLICY", "/srv/brain/app/policy.yaml")),
        branch=env.get("BRAIN_BRANCH", "main"),
        public_url=public_url,
        public_host=env.get("BRAIN_PUBLIC_BIND", "127.0.0.1"),
        public_port=int(env.get("BRAIN_PUBLIC_PORT", "8787")),
        internal_host=env.get("BRAIN_INTERNAL_BIND", "127.0.0.1"),
        internal_port=int(env.get("BRAIN_INTERNAL_PORT", "8788")),
        allowed_hosts=sorted(set(allowed_hosts)),
        github_client_id=creds.get("GITHUB_CLIENT_ID") or env.get("GITHUB_CLIENT_ID"),
        github_client_secret=creds.get("GITHUB_CLIENT_SECRET"),
    )
