"""Authentification : jetons statiques (entrée interne) et OAuth GitHub (entrée publique).

Dans les deux cas, le jeton vérifié porte la claim `brain_client`, le nom du
client de policy.yaml dont les droits s'appliquent.
"""

from __future__ import annotations

import logging

from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubTokenVerifier

from .policy import Policy

log = logging.getLogger(__name__)

# URL de rappel des clients MCP autorisés à s'enregistrer auprès du proxy OAuth.
# Claude sur le web (claude.ai et claude.com) et les clients locaux (Claude Desktop, Claude Code).
ALLOWED_CLIENT_REDIRECTS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://claude.ai/api/organizations/*/custom-connectors/oauth/callback",
    "http://localhost:*",
    "http://127.0.0.1:*",
]


class PolicyTokenVerifier(TokenVerifier):
    """Jetons statiques : le hash du jeton présenté doit figurer dans policy.yaml."""

    def __init__(self, policy: Policy):
        super().__init__()
        self.policy = policy

    async def verify_token(self, token: str) -> AccessToken | None:
        client = self.policy.by_token(token)
        if client is None:
            return None
        return AccessToken(
            token=token,
            client_id=client.name,
            scopes=[],
            subject=client.name,
            claims={"brain_client": client.name},
        )


class AllowlistedGitHubVerifier(GitHubTokenVerifier):
    """Vérifie le jeton auprès de GitHub, puis exige un compte présent dans policy.yaml."""

    def __init__(self, policy: Policy, **kwargs):
        super().__init__(**kwargs)
        self.policy = policy

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await super().verify_token(token)
        if result is None:
            return None
        client = self.policy.by_github_id(str(result.subject))
        if client is None:
            log.warning("compte GitHub refusé (absent de la politique) : id=%s", result.subject)
            return None
        claims = dict(result.claims or {})
        claims.pop("github_user_data", None)
        claims["brain_client"] = client.name
        return result.model_copy(update={"claims": claims})


def github_oauth(policy: Policy, *, client_id: str, client_secret: str, base_url: str) -> OAuthProxy:
    verifier = AllowlistedGitHubVerifier(
        policy,
        required_scopes=["read:user"],
        cache_ttl_seconds=300,
    )
    return OAuthProxy(
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id=client_id,
        upstream_client_secret=client_secret,
        token_verifier=verifier,
        base_url=base_url,
        issuer_url=base_url,
        allowed_client_redirect_uris=ALLOWED_CLIENT_REDIRECTS,
    )
