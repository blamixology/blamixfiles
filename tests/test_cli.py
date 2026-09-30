import pytest

from blamixfiles import cli
from blamixfiles.core import ssh
from blamixfiles.models import Site, open_or_create
from blamixfiles.paths import vault_path
from servers import PASSWORD, USER, SFTPTestServer


def run(capsys, *args) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as ei:
        cli.main(list(args))
    out = capsys.readouterr()
    cli._store_cache.clear()
    return ei.value.code, out.out, out.err


def test_cli_roundtrip(tmp_path, capsys, monkeypatch):
    root = tmp_path / "srv"
    (root / "www").mkdir(parents=True)
    (root / "www" / "a.txt").write_text("hello")
    with SFTPTestServer(root) as srv:
        site = Site(name="web", protocol="sftp", host="127.0.0.1", port=srv.port,
                    username=USER, password=PASSWORD)
        open_or_create(vault_path(), "master", create=True).upsert(site)
        monkeypatch.setenv("BLAMIXFILES_VAULT_PASSWORD", "master")
        # unknown host key without a terminal -> refused, exit code 3
        code, _, err = run(capsys, "ls", "web:/www")
        assert code == 3 and "not trusted" in err
        try:
            ssh.open_client(site)
        except ssh.UnknownHostKey as e:
            ssh.trust_host_key(e.host_id, e.key)

        code, out, _ = run(capsys, "sites")
        assert code == 0 and "web" in out
        code, out, _ = run(capsys, "ls", "web:/www")
        assert code == 0 and "a.txt" in out

        dl = tmp_path / "dl"
        code, out, _ = run(capsys, "get", "web:/www", str(dl))
        assert code == 0 and (dl / "www" / "a.txt").read_text() == "hello"

        up = tmp_path / "up.txt"
        up.write_text("uploaded")
        url = f"sftp://{USER}:{PASSWORD}@127.0.0.1:{srv.port}/www"
        code, out, _ = run(capsys, "put", str(up), url)
        assert code == 0 and (root / "www" / "up.txt").read_text() == "uploaded"

        code, _, err = run(capsys, "ls", "nosuch:/")
        assert code == 3 and "No saved site" in err
        monkeypatch.setenv("BLAMIXFILES_VAULT_PASSWORD", "wrong")
        code, _, err = run(capsys, "sites")
        assert code == 3 and "Wrong master password" in err


def test_cli_sync(tmp_path, capsys, monkeypatch):
    from blamixfiles.core import sync as S
    root = tmp_path / "srv"
    (root / "www").mkdir(parents=True)
    (root / "www" / "stale.txt").write_text("old")
    local = tmp_path / "dist"
    local.mkdir()
    (local / "app.js").write_text("console.log(1)")
    with SFTPTestServer(root) as srv:
        site = Site(name="web", protocol="sftp", host="127.0.0.1", port=srv.port,
                    username=USER, password=PASSWORD)
        store = open_or_create(vault_path(), "master", create=True)
        store.upsert(site)
        store.save_profile(S.SyncProfile("deploy", site.id, str(local), "/www",
                                         S.SyncOptions(mirror=True)).to_dict())
        monkeypatch.setenv("BLAMIXFILES_VAULT_PASSWORD", "master")
        try:
            ssh.open_client(site)
        except ssh.UnknownHostKey as e:
            ssh.trust_host_key(e.host_id, e.key)

        code, out, _ = run(capsys, "sync", "deploy", "--dry-run")
        assert code == 0 and "Upload" in out and "Delete on server" in out
        assert not (root / "www" / "app.js").exists()             # dry run changed nothing

        code, _, err = run(capsys, "sync", "deploy")              # deletes need --yes in scripts
        assert code == 3 and "--yes" in err and (root / "www" / "stale.txt").exists()

        code, out, _ = run(capsys, "sync", "deploy", "--yes")
        assert code == 0 and (root / "www" / "app.js").exists() and not (root / "www" / "stale.txt").exists()

        code, out, _ = run(capsys, "sync", str(local), "web:/www", "--json")
        import json
        assert code == 0 and json.loads(out)["actions"] == []     # in sync now

        code, out, _ = run(capsys, "profiles")
        assert "deploy" in out and "[mirror]" in out
