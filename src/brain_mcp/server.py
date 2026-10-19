"""Serveur MCP : outils du vault, exposés sur deux entrées.

- entrée publique (Tailscale Funnel) : OAuth GitHub, pour les connecteurs hébergés ;
- entrée interne (localhost / Tailscale Serve) : jetons statiques, pour Hermes et les outils locaux.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from pydantic import Field

from . import config
from .auth import PolicyTokenVerifier, github_oauth
from .policy import Client, Policy
from .vault import Vault, VaultError

log = logging.getLogger("brain_mcp")

INSTRUCTIONS = """\
Base de connaissance personnelle (vault Markdown, compatible Obsidian, versionné dans git).
Structure : 00-inbox (captures à trier), 10-projects, 20-areas, 30-resources, 40-archives.
Chaque dossier a un README.md qui décrit son usage. Commencer par whoami pour connaître ses droits.
Les notes ont un en-tête YAML (title, created, tags, source). Pas de suppression : archiver avec move_note.
"""

NotePath = Annotated[str, Field(description="Chemin relatif au vault, par exemple 10-projects/demo/plan.md")]


def build_server(name: str, auth: AuthProvider, vault: Vault, policy: Policy) -> FastMCP:
    mcp = FastMCP(name=name, instructions=INSTRUCTIONS, auth=auth)
    mcp.add_middleware(RateLimitingMiddleware(max_requests_per_second=5, burst_capacity=20))

    def caller() -> Client:
        token = get_access_token()
        name = (token.claims or {}).get("brain_client") if token else None
        client = policy.clients.get(name) if name else None
        if client is None:
            raise ToolError("client non autorisé")
        return client

    def guarded(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except VaultError as exc:
            raise ToolError(str(exc)) from exc

    async def guarded_async(coro):
        try:
            result = await coro
        except VaultError as exc:
            raise ToolError(str(exc)) from exc
        return result.as_dict()

    @mcp.tool(annotations={"readOnlyHint": True})
    def whoami() -> dict:
        """Nom du client connecté et chemins qu'il peut lire ou écrire."""
        return caller().summary()

    @mcp.tool(annotations={"readOnlyHint": True})
    def list_notes(folder: Annotated[str, Field(description="Dossier de départ, vide pour tout le vault")] = "") -> list[str]:
        """Liste les notes Markdown lisibles, récursivement depuis un dossier."""
        return guarded(vault.list_notes, caller(), folder)

    @mcp.tool(annotations={"readOnlyHint": True})
    def read_note(path: NotePath) -> str:
        """Renvoie le contenu complet d'une note, en-tête YAML compris."""
        return guarded(vault.read, caller(), path)

    @mcp.tool(annotations={"readOnlyHint": True})
    def search_notes(
        query: Annotated[str, Field(description="Texte à chercher, sans tenir compte de la casse")],
        folder: Annotated[str, Field(description="Limiter à un dossier")] = "",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> list[dict]:
        """Recherche plein texte dans les notes lisibles. Renvoie chemins et lignes trouvées."""
        return guarded(vault.search, caller(), query, folder, limit)

    @mcp.tool(annotations={"readOnlyHint": True})
    def note_history(path: NotePath, limit: Annotated[int, Field(ge=1, le=50)] = 10) -> list[dict]:
        """Historique git d'une note : qui l'a modifiée, quand, avec quel message."""
        return guarded(vault.history, caller(), path, limit)

    @mcp.tool(annotations={"destructiveHint": False})
    async def create_note(
        path: NotePath,
        content: Annotated[str, Field(description="Corps de la note en Markdown")],
        title: Annotated[str | None, Field(description="Titre, par défaut le nom du fichier")] = None,
        tags: Annotated[list[str] | None, Field(description="Étiquettes")] = None,
    ) -> dict:
        """Crée une note. Ajoute un en-tête YAML si le contenu n'en a pas. Échoue si elle existe."""
        return await guarded_async(vault.create(caller(), path, content, title, tags))

    @mcp.tool(annotations={"destructiveHint": True})
    async def update_note(path: NotePath, content: Annotated[str, Field(description="Nouveau contenu complet")]) -> dict:
        """Remplace le contenu d'une note existante. L'ancienne version reste dans l'historique git."""
        return await guarded_async(vault.update(caller(), path, content))

    @mcp.tool(annotations={"destructiveHint": False})
    async def append_to_note(path: NotePath, text: Annotated[str, Field(description="Texte ajouté à la fin")]) -> dict:
        """Ajoute du texte à la fin d'une note existante."""
        return await guarded_async(vault.append(caller(), path, text))

    @mcp.tool(annotations={"destructiveHint": False})
    async def move_note(source: NotePath, destination: NotePath) -> dict:
        """Déplace ou renomme une note (par exemple vers 40-archives pour archiver)."""
        return await guarded_async(vault.move(caller(), source, destination))

    return mcp


def build_apps(settings: config.Settings):
    policy = Policy.load(settings.policy_file)
    vault = Vault(settings.vault, branch=settings.branch)
    apps = []

    internal = build_server("brain", PolicyTokenVerifier(policy), vault, policy)
    apps.append(("interne", settings.internal_host, settings.internal_port,
                 internal.http_app(path="/mcp", allowed_hosts=settings.allowed_hosts)))

    if settings.public_enabled:
        oauth = github_oauth(policy, client_id=settings.github_client_id,
                             client_secret=settings.github_client_secret,
                             base_url=settings.public_url)
        public = build_server("brain", oauth, vault, policy)
        apps.append(("publique", settings.public_host, settings.public_port,
                     public.http_app(path="/mcp", allowed_hosts=settings.allowed_hosts)))
    else:
        log.warning("entrée publique désactivée : BRAIN_PUBLIC_URL ou identifiants OAuth absents")
    return policy, apps


async def serve(settings: config.Settings) -> None:
    policy, apps = build_apps(settings)
    log.info("politique chargée : %s", ", ".join(sorted(policy.clients)))
    servers = []
    for label, host, port, app in apps:
        log.info("entrée %s sur http://%s:%d/mcp", label, host, port)
        servers.append(uvicorn.Server(uvicorn.Config(
            app, host=host, port=port, log_level="info",
            proxy_headers=True, forwarded_allow_ips="127.0.0.1",
        )))
    await asyncio.gather(*(s.serve() for s in servers))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(serve(config.load()))


if __name__ == "__main__":
    main()
