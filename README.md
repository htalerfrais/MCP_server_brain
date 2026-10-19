# MCP_server_brain

Serveur [MCP](https://modelcontextprotocol.io) d'une base de connaissance personnelle. Le contenu
est un vault Markdown compatible Obsidian, versionné dans le dépôt privé `brain-vault`. Chaque
écriture devient un commit dont l'auteur est le client qui l'a faite.

## Architecture

```
Claude sur le web ──HTTPS + OAuth GitHub──► Tailscale Funnel ─► 127.0.0.1:8787  entrée publique
Hermes, outils locaux ──jeton statique─────────────────────────► 127.0.0.1:8788  entrée interne
                                                                      │
                                                    service systemd brain-mcp (user brain)
                                                                      │
                                         /srv/brain/vault  (clone de brain-vault, git push)
```

| Élément | Emplacement |
|---|---|
| Code (ce dépôt) | `/srv/brain/app`, propriété de `hector`, lecture seule pour `brain` |
| Environnement Python | `/srv/brain/app/.venv`, construit par `uv sync --frozen` |
| Vault | `/srv/brain/vault`, propriété de `brain` |
| État (stockage OAuth) | `/srv/brain/state` |
| Secret OAuth GitHub | `/etc/brain/github_oauth`, root seul, transmis par `LoadCredential=` |
| Politique d'accès | `policy.yaml` (versionné, sans secret) |
| Unité systemd | `deploy/brain-mcp.service` |

- **Entrée publique** : OAuth via une OAuth App GitHub (proxy OAuth de FastMCP). Seuls les comptes
  GitHub listés dans `policy.yaml` sont acceptés, identifiés par leur id numérique.
- **Entrée interne** : jetons statiques. Seule leur empreinte SHA-256 est stockée.
- **Droits** : chaque client a des motifs de chemins en lecture et en écriture. Écrire implique lire.
  Aucune suppression : on archive avec `move_note`.

## Outils MCP

| Outil | Rôle |
|---|---|
| `whoami` | Client connecté et ses droits |
| `list_notes` | Notes lisibles d'un dossier |
| `read_note` | Contenu d'une note |
| `search_notes` | Recherche plein texte |
| `note_history` | Historique git d'une note |
| `create_note` | Création, avec en-tête YAML ajouté si absent |
| `update_note` | Remplacement du contenu |
| `append_to_note` | Ajout en fin de note |
| `move_note` | Déplacement ou archivage |

## Exploitation

Ajouter un client à jeton :

```bash
.venv/bin/brain-token hermes      # affiche le jeton une fois, et l'entrée à copier
# coller l'entrée dans policy.yaml, adapter read/write, commit sur dev, fusion dans main
scripts/deploy.sh                 # déploie main et redémarre le service
```

Déployer une branche : `scripts/deploy.sh [branche]` (par défaut `main`).

Journaux : `journalctl -u brain-mcp -f`.

## Développement

```bash
uv sync
uv run pytest
```

Le travail se fait sur la branche `dev`. `main` est la branche déployée.
