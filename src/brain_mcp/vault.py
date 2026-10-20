"""Accès au vault : chemins sûrs, lecture, recherche, écritures commitées dans git."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from .policy import Client

log = logging.getLogger(__name__)

MAX_NOTE_BYTES = 1_000_000
SKIP_DIRS = {".git", ".obsidian", ".trash"}
TRASH = ".corbeille"
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class VaultError(Exception):
    """Erreur présentable au client (chemin refusé, note absente...)."""


@dataclass
class CommitResult:
    changed: bool
    commit: str | None = None
    pushed: bool = False
    warning: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


class Vault:
    def __init__(self, root: Path, branch: str = "main", push: bool = True):
        self.root = root.resolve()
        self.branch = branch
        self.push_enabled = push
        self._lock = asyncio.Lock()

    # ---------- chemins ----------

    def rel(self, path: str, *, folder: bool = False) -> str:
        """Normalise un chemin relatif au vault et refuse tout ce qui en sort."""
        raw = (path or "").strip().replace("\\", "/")
        if raw.startswith("/"):
            raise VaultError("chemin absolu refusé : donner un chemin relatif au vault")
        parts = [p for p in PurePosixPath(raw).parts if p not in ("", ".")]
        for part in parts:
            if part == ".." or part.startswith("."):
                raise VaultError(f"segment de chemin refusé : {part!r}")
        rel = "/".join(parts)
        if not folder:
            if not rel:
                raise VaultError("chemin vide")
            if not rel.endswith(".md"):
                raise VaultError("seules les notes Markdown (.md) sont gérées")
        absolute = (self.root / rel).resolve()
        if absolute != self.root and self.root not in absolute.parents:
            raise VaultError("chemin hors du vault")
        return rel

    def _abs(self, rel: str) -> Path:
        return self.root / rel

    # ---------- lecture ----------

    def iter_notes(self, folder: str = ""):
        base = self._abs(self.rel(folder, folder=True))
        if not base.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if name.endswith(".md") and not name.startswith("."):
                    full = Path(dirpath) / name
                    yield full.relative_to(self.root).as_posix()

    def list_notes(self, client: Client, folder: str = "") -> list[str]:
        return [p for p in self.iter_notes(folder) if client.can_read(p)]

    def read(self, client: Client, path: str) -> str:
        rel = self.rel(path)
        if not client.can_read(rel):
            raise VaultError(f"lecture refusée pour {client.name} : {rel}")
        target = self._abs(rel)
        if not target.is_file():
            raise VaultError(f"note introuvable : {rel}")
        return target.read_text(encoding="utf-8")

    def search(self, client: Client, query: str, folder: str = "", limit: int = 20) -> list[dict]:
        needle = query.strip().lower()
        if len(needle) < 2:
            raise VaultError("la recherche demande au moins 2 caractères")
        limit = max(1, min(limit, 100))
        results = []
        for rel in self.list_notes(client, folder):
            target = self._abs(rel)
            if target.stat().st_size > MAX_NOTE_BYTES:
                continue
            text = target.read_text(encoding="utf-8", errors="replace")
            hits = []
            for lineno, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append({"line": lineno, "text": line.strip()[:240]})
                    if len(hits) == 3:
                        break
            if hits or needle in rel.lower():
                results.append({"path": rel, "matches": hits})
                if len(results) >= limit:
                    break
        return results

    # ---------- git ----------

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, check=check, timeout=60,
        )

    def has_remote(self) -> bool:
        return bool(self._git("remote", check=False).stdout.strip())

    def history(self, client: Client, path: str, limit: int = 10) -> list[dict]:
        rel = self.rel(path)
        if not client.can_read(rel):
            raise VaultError(f"lecture refusée pour {client.name} : {rel}")
        out = self._git("log", f"-n{max(1, min(limit, 50))}", "--follow",
                        "--format=%H%x1f%an%x1f%aI%x1f%s", "--", rel, check=False).stdout
        entries = []
        for line in out.splitlines():
            sha, author, date, subject = line.split("\x1f", 3)
            entries.append({"commit": sha[:10], "author": author, "date": date, "message": subject})
        return entries

    def _sync_before_write(self) -> str | None:
        if not (self.push_enabled and self.has_remote()):
            return None
        pulled = self._git("pull", "--rebase", "--quiet", "origin", self.branch, check=False)
        if pulled.returncode != 0:
            self._git("rebase", "--abort", check=False)
            return "synchronisation avec GitHub impossible avant l'écriture : " + pulled.stderr.strip()[:200]
        return None

    def _commit_sync(self, client: Client, message: str, paths: list[str],
                     mutate: Callable[[], None]) -> CommitResult:
        warning = self._sync_before_write()
        mutate()
        self._git("add", "-A", "--", *paths)
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return CommitResult(changed=False, warning=warning)
        self._git(
            "-c", "user.name=brain-mcp", "-c", "user.email=brain-mcp@localhost",
            "commit", "--quiet",
            "--author", f"{client.name} <{client.name}@brain-mcp.local>",
            "-m", message,
        )
        sha = self._git("rev-parse", "--short", "HEAD").stdout.strip()
        pushed = False
        if self.push_enabled and self.has_remote():
            pushed_run = self._git("push", "--quiet", "origin", f"HEAD:{self.branch}", check=False)
            pushed = pushed_run.returncode == 0
            if not pushed:
                log.warning("push échoué : %s", pushed_run.stderr.strip())
                warning = "commit local fait, push vers GitHub échoué : il sera retenté à la prochaine écriture"
        return CommitResult(changed=True, commit=sha, pushed=pushed, warning=warning)

    async def commit(self, client: Client, message: str, paths: list[str],
                     mutate: Callable[[], None]) -> CommitResult:
        async with self._lock:
            return await asyncio.to_thread(self._commit_sync, client, message, paths, mutate)

    # ---------- écriture ----------

    def _check_write(self, client: Client, rel: str) -> None:
        if not client.can_write(rel):
            raise VaultError(f"écriture refusée pour {client.name} : {rel}")

    @staticmethod
    def _check_size(content: str) -> None:
        if len(content.encode()) > MAX_NOTE_BYTES:
            raise VaultError("note trop volumineuse (1 Mo maximum)")

    @staticmethod
    def with_frontmatter(content: str, title: str, tags: list[str] | None, source: str) -> str:
        if content.startswith("---\n"):
            return content
        lines = [
            "---",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            f"created: {dt.date.today().isoformat()}",
            f"tags: {json.dumps(tags or [], ensure_ascii=False)}",
            f"source: {source}",
            "---",
            "",
        ]
        return "\n".join(lines) + content.lstrip("\n")

    async def create(self, client: Client, path: str, content: str,
                     title: str | None = None, tags: list[str] | None = None) -> CommitResult:
        rel = self.rel(path)
        self._check_write(client, rel)
        body = self.with_frontmatter(content, title or PurePosixPath(rel).stem, tags, client.name)
        self._check_size(body)
        target = self._abs(rel)

        def mutate():
            if target.exists():
                raise VaultError(f"la note existe déjà : {rel} (utiliser update_note)")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")

        return await self.commit(client, f"Création de {rel}", [rel], mutate)

    async def update(self, client: Client, path: str, content: str) -> CommitResult:
        rel = self.rel(path)
        self._check_write(client, rel)
        self._check_size(content)
        target = self._abs(rel)

        def mutate():
            if not target.is_file():
                raise VaultError(f"note introuvable : {rel} (utiliser create_note)")
            target.write_text(content, encoding="utf-8")

        return await self.commit(client, f"Mise à jour de {rel}", [rel], mutate)

    async def append(self, client: Client, path: str, text: str) -> CommitResult:
        rel = self.rel(path)
        self._check_write(client, rel)
        target = self._abs(rel)

        def mutate():
            if not target.is_file():
                raise VaultError(f"note introuvable : {rel}")
            current = target.read_text(encoding="utf-8")
            joined = current + ("" if current.endswith("\n") else "\n") + text.rstrip("\n") + "\n"
            self._check_size(joined)
            target.write_text(joined, encoding="utf-8")

        return await self.commit(client, f"Ajout dans {rel}", [rel], mutate)

    async def move(self, client: Client, source: str, destination: str) -> CommitResult:
        src, dst = self.rel(source), self.rel(destination)
        self._check_write(client, src)
        self._check_write(client, dst)
        src_abs, dst_abs = self._abs(src), self._abs(dst)

        def mutate():
            if not src_abs.is_file():
                raise VaultError(f"note introuvable : {src}")
            if dst_abs.exists():
                raise VaultError(f"la destination existe déjà : {dst}")
            dst_abs.parent.mkdir(parents=True, exist_ok=True)
            src_abs.rename(dst_abs)

        return await self.commit(client, f"Déplacement de {src} vers {dst}", [src, dst], mutate)

    # ---------- structure (curateurs) ----------

    @staticmethod
    def _check_curator(client: Client) -> None:
        if not client.is_curator:
            raise VaultError(f"réservé aux curateurs : {client.name} est contributeur")

    def tree(self, client: Client) -> dict[str, int]:
        """Nombre de notes lisibles par dossier, pour voir la structure d'un coup."""
        counts: dict[str, int] = {}
        for rel in self.list_notes(client):
            folder = str(PurePosixPath(rel).parent)
            counts[folder if folder != "." else "/"] = counts.get(folder if folder != "." else "/", 0) + 1
        return dict(sorted(counts.items()))

    async def move_folder(self, client: Client, source: str, destination: str) -> CommitResult:
        self._check_curator(client)
        src, dst = self.rel(source, folder=True), self.rel(destination, folder=True)
        if not src or not dst:
            raise VaultError("le dossier racine ne peut pas être déplacé")
        if dst == src or dst.startswith(src + "/"):
            raise VaultError("la destination ne peut pas être dans la source")
        src_abs, dst_abs = self._abs(src), self._abs(dst)

        def mutate():
            if not src_abs.is_dir():
                raise VaultError(f"dossier introuvable : {src}")
            if dst_abs.exists():
                raise VaultError(f"la destination existe déjà : {dst}")
            dst_abs.parent.mkdir(parents=True, exist_ok=True)
            src_abs.rename(dst_abs)

        return await self.commit(client, f"Déplacement du dossier {src} vers {dst}", [src, dst], mutate)

    async def delete(self, client: Client, path: str) -> CommitResult:
        """Met la note à la corbeille (`.corbeille/`, cachée) ; elle reste dans git."""
        self._check_curator(client)
        rel = self.rel(path)
        src_abs = self._abs(rel)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        trash_rel = f"{TRASH}/{rel}"

        def mutate():
            if not src_abs.is_file():
                raise VaultError(f"note introuvable : {rel}")
            dst_abs = self._abs(trash_rel)
            if dst_abs.exists():
                dst_abs = dst_abs.with_name(f"{dst_abs.stem}.{stamp}.md")
            dst_abs.parent.mkdir(parents=True, exist_ok=True)
            src_abs.rename(dst_abs)

        return await self.commit(client, f"Suppression de {rel} (corbeille)", [rel, TRASH], mutate)

    async def revert(self, client: Client, commit: str) -> CommitResult:
        """Annule un commit du vault par un nouveau commit (restaure aussi une note supprimée)."""
        self._check_curator(client)
        sha = commit.strip().lower()
        if not _SHA.match(sha):
            raise VaultError("identifiant de commit invalide")

        def mutate():
            known = self._git("cat-file", "-t", sha, check=False)
            if known.returncode != 0 or known.stdout.strip() != "commit":
                raise VaultError(f"commit introuvable : {sha}")
            if len(self._git("rev-list", "--parents", "-n1", sha).stdout.split()) > 2:
                raise VaultError("un commit de fusion ne peut pas être annulé")
            run = self._git("-c", "user.name=brain-mcp", "-c", "user.email=brain-mcp@localhost",
                            "revert", "--no-commit", sha, check=False)
            if run.returncode != 0:
                self._git("revert", "--abort", check=False)
                self._git("reset", "--hard", "--quiet", "HEAD", check=False)
                raise VaultError("annulation impossible (conflit avec des changements plus récents)")

        return await self.commit(client, f"Annulation du commit {sha[:10]}", ["."], mutate)
