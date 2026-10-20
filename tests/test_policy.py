import pytest

from brain_mcp.policy import Policy, PolicyError, hash_token, matches


def test_glob_patterns():
    assert matches(("**",), "a/b/c.md")
    assert matches(("00-inbox/**",), "00-inbox/x.md")
    assert matches(("00-inbox/**",), "00-inbox/sub/x.md")
    assert not matches(("00-inbox/**",), "10-projects/x.md")
    assert matches(("*.md",), "root.md")
    assert not matches(("*.md",), "dir/note.md")
    assert matches(("**/README.md",), "README.md")
    assert matches(("**/README.md",), "a/b/README.md")


def write_policy(tmp_path, text):
    path = tmp_path / "policy.yaml"
    path.write_text(text)
    return path


def test_load_and_lookup(tmp_path):
    digest = hash_token("secret-token")
    policy = Policy.load(write_policy(tmp_path, f"""
clients:
  owner: {{auth: github, github_ids: [42], read: ["**"], write: ["**"]}}
  hermes: {{auth: token, token_sha256: {digest}, read: ["**"], write: ["00-inbox/**"]}}
"""))
    assert policy.by_github_id("42").name == "owner"
    assert policy.by_github_id("43") is None
    hermes = policy.by_token("secret-token")
    assert hermes.name == "hermes"
    assert policy.by_token("wrong") is None
    assert hermes.can_write("00-inbox/a.md")
    assert not hermes.can_write("10-projects/a.md")
    assert hermes.can_read("10-projects/a.md")


@pytest.mark.parametrize("bad", [
    "clients: {}",
    "clients:\n  x: {auth: token, token_sha256: abc, read: [], write: []}",
    "clients:\n  x: {auth: github, read: [], write: []}",
    "clients:\n  x: {auth: github, github_ids: [1], read: ['../etc'], write: []}",
    "clients:\n  x: {auth: github, github_ids: [1], read: [], write: [], extra: 1}",
    "clients:\n  x: {auth: github, github_ids: [1], role: curator, write: ['**']}",
    "clients:\n  x: {auth: github, github_ids: [1], role: boss}",
    "clients:\n  Bad_Name: {auth: github, github_ids: [1], read: [], write: []}",
])
def test_invalid_policies(tmp_path, bad):
    with pytest.raises(PolicyError):
        Policy.load(write_policy(tmp_path, bad))


def test_curator_has_everything(tmp_path):
    policy = Policy.load(write_policy(tmp_path, """
clients:
  owner: {auth: github, github_ids: [42], role: curator}
  bot: {auth: github, github_ids: [43], write: ["00-inbox/**"]}
"""))
    owner, bot = policy.by_github_id("42"), policy.by_github_id("43")
    assert owner.is_curator and owner.can_write("any/where.md")
    assert not bot.is_curator and bot.role == "contributor"
