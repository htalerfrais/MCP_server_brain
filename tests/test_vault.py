import subprocess

import pytest

from brain_mcp.policy import Client
from brain_mcp.vault import Vault, VaultError

ADMIN = Client("admin", "token", ("**",), ("**",))
INBOX = Client("hermes", "token", ("**",), ("00-inbox/**",))
READER = Client("reader", "token", ("10-projects/**",), ())


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def vault(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    root = tmp_path / "vault"
    subprocess.run(["git", "clone", "-q", str(remote), str(root)], check=True, capture_output=True)
    (root / "README.md").write_text("# Vault\n")
    git(root, "add", "README.md")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    git(root, "push", "-q", "origin", "HEAD:main")
    return Vault(root)


@pytest.mark.parametrize("path", ["/etc/passwd.md", "../x.md", "a/../../x.md", ".git/config.md",
                                  ".obsidian/x.md", "note.txt", ""])
def test_rejects_bad_paths(vault, path):
    with pytest.raises(VaultError):
        vault.rel(path)


def test_symlink_outside_is_refused(vault, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("secret")
    (vault.root / "link.md").symlink_to(outside)
    with pytest.raises(VaultError):
        vault.read(ADMIN, "link.md")


async def test_create_read_search_history(vault):
    result = await vault.create(ADMIN, "10-projects/demo/plan.md", "Hello world\n", title="Plan", tags=["demo"])
    assert result.changed and result.pushed
    text = vault.read(ADMIN, "10-projects/demo/plan.md")
    assert text.startswith("---\ntitle: \"Plan\"")
    assert "source: admin" in text
    assert vault.search(ADMIN, "hello")[0]["path"] == "10-projects/demo/plan.md"
    history = vault.history(ADMIN, "10-projects/demo/plan.md")
    assert history[0]["author"] == "admin"
    remote_log = git(vault.root.parent / "remote.git", "log", "--format=%an %s", "main")
    assert "admin Création de 10-projects/demo/plan.md" in remote_log


async def test_create_twice_fails(vault):
    await vault.create(ADMIN, "a.md", "x")
    with pytest.raises(VaultError):
        await vault.create(ADMIN, "a.md", "y")


async def test_permissions(vault):
    await vault.create(INBOX, "00-inbox/idea.md", "idée")
    with pytest.raises(VaultError):
        await vault.create(INBOX, "10-projects/x.md", "non")
    await vault.create(ADMIN, "20-areas/private.md", "privé")
    assert "20-areas/private.md" not in vault.list_notes(READER)
    with pytest.raises(VaultError):
        vault.read(READER, "20-areas/private.md")
    assert vault.search(READER, "privé") == []


async def test_update_append_move(vault):
    await vault.create(ADMIN, "00-inbox/n.md", "un")
    await vault.update(ADMIN, "00-inbox/n.md", "deux\n")
    await vault.append(ADMIN, "00-inbox/n.md", "trois")
    assert vault.read(ADMIN, "00-inbox/n.md") == "deux\ntrois\n"
    await vault.move(ADMIN, "00-inbox/n.md", "40-archives/n.md")
    assert vault.list_notes(ADMIN, "40-archives") == ["40-archives/n.md"]
    with pytest.raises(VaultError):
        await vault.update(ADMIN, "00-inbox/n.md", "x")
