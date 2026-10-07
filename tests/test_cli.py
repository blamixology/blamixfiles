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


def test_cli_mkdir_mv_rm_and_json(tmp_path, capsys, monkeypatch):
    import json
    root = tmp_path / "srv"
    (root / "www").mkdir(parents=True)
    (root / "www" / "a.txt").write_text("hello")
    with SFTPTestServer(root) as srv:
        site = Site(name="web", protocol="sftp", host="127.0.0.1", port=srv.port,
                    username=USER, password=PASSWORD)
        open_or_create(vault_path(), "master", create=True).upsert(site)
        monkeypatch.setenv("BLAMIXFILES_VAULT_PASSWORD", "master")
        try:
            ssh.open_client(site)
        except ssh.UnknownHostKey as e:
            ssh.trust_host_key(e.host_id, e.key)

        assert run(capsys, "mkdir", "web:/www/new/deeper")[0] == 0
        assert (root / "www" / "new" / "deeper").is_dir()
        assert run(capsys, "mv", "web:/www/a.txt", "/www/new/b.txt")[0] == 0
        assert (root / "www" / "new" / "b.txt").read_text() == "hello" and not (root / "www" / "a.txt").exists()

        code, _, err = run(capsys, "rm", "web:/www/new")                     # folder without -r
        assert code == 3 and "-r" in err
        code, _, err = run(capsys, "rm", "-r", "web:/www/new")                # in a script: needs --yes
        assert code == 3 and "--yes" in err
        assert run(capsys, "rm", "-r", "--yes", "web:/www/new")[0] == 0
        assert not (root / "www" / "new").exists()
        assert run(capsys, "rm", "web:/")[0] == 3

        up = tmp_path / "up.txt"
        up.write_text("x")
        code, out, err = run(capsys, "put", "--json", str(up), "web:/www")
        assert out.strip(), err
        doc = json.loads(out)
        assert code == 0 and doc["result"]["done"] == 1 and doc["result"]["failed"] == []

        local = tmp_path / "local"
        local.mkdir()
        (local / "n.txt").write_text("new")
        code, out, err = run(capsys, "sync", "--json", str(local), "web:/www")
        assert out.strip(), (code, err)
        doc = json.loads(out)
        assert code == 0 and doc["actions"] and doc["result"]["done"] >= 1
        assert (root / "www" / "n.txt").read_text() == "new"


def test_cli_find_du_keygen_copy_id_schedule(tmp_path, capsys, monkeypatch):
    import json
    root = tmp_path / "srv"
    (root / "www" / "logs").mkdir(parents=True)
    (root / "www" / "logs" / "a.log").write_bytes(b"x" * 3000)
    (root / "www" / "index.html").write_text("hi")
    with SFTPTestServer(root) as srv:
        site = Site(name="web", protocol="sftp", host="127.0.0.1", port=srv.port,
                    username=USER, password=PASSWORD)
        st = open_or_create(vault_path(), "master", create=True)
        st.upsert(site)
        st.save_profile({"name": "Deploy", "site_id": site.id, "local_dir": str(tmp_path), "remote_dir": "/www",
                         "options": {}})
        monkeypatch.setenv("BLAMIXFILES_VAULT_PASSWORD", "master")
        try:
            ssh.open_client(site)
        except ssh.UnknownHostKey as e:
            ssh.trust_host_key(e.host_id, e.key)

        code, out, _ = run(capsys, "find", "web:/www", "--name", "*.log", "--json")
        assert code == 0 and [h["path"] for h in json.loads(out)] == ["/www/logs/a.log"]
        assert run(capsys, "find", "web:/www", "--name", "nothing-like-this")[0] == 1
        assert run(capsys, "find", "web:/www", "--larger", "huge")[0] == 3
        code, out, _ = run(capsys, "du", "web:/www", "--json")
        assert code == 0 and json.loads(out) == {"bytes": 3002, "files": 2, "folders": 1, "unreadable": 0}

        key = tmp_path / "id_cli"
        code, out, _ = run(capsys, "keygen", str(key), "--no-passphrase", "--comment", "cli-test")
        assert code == 0 and (tmp_path / "id_cli.pub").exists() and "cli-test" in out
        code, out, _ = run(capsys, "copy-id", "web", "--key", str(key) + ".pub")
        assert code == 0 and "Added" in out
        code, out, _ = run(capsys, "copy-id", "web", "--key", str(key) + ".pub")
        assert code == 0 and "Already there" in out

        from blamixfiles.core import schedule as SC
        calls = []
        monkeypatch.setattr(SC, "available", lambda: True)
        monkeypatch.setattr(SC, "install", lambda name, when: calls.append((name, when)))
        monkeypatch.setattr(SC, "listing", lambda: {"Deploy": "every day at 02:00"})
        code, out, err = run(capsys, "schedule", "Deploy", "--daily", "02:00")
        assert code == 0 and calls[0][0] == "Deploy" and calls[0][1].daily == "02:00" and "keychain" in err
        assert run(capsys, "schedule", "Nope", "--daily", "02:00")[0] == 3
        assert run(capsys, "schedule", "Deploy", "--daily", "26:00")[0] == 3
        code, out, _ = run(capsys, "schedule", "--list")
        assert code == 0 and "Deploy" in out and "02:00" in out

        code, out, _ = run(capsys, "diagnostics")
        assert code == 0 and "BlamixFiles" in out and "Python" in out


def test_packaged_app_runs_cli_commands(monkeypatch, capsys):
    """BlamixFiles.exe sync … (a scheduled task) goes to the command line, not the window."""
    from blamixfiles import cli as C
    from blamixfiles import main as M
    seen = []
    monkeypatch.setattr(C, "main", lambda argv: seen.append(argv))
    monkeypatch.setattr(M.sys, "argv", ["BlamixFiles.exe", "sync", "Deploy", "--yes"])
    assert M._cli_from_app() and seen == [["sync", "Deploy", "--yes"]]
    monkeypatch.setattr(M.sys, "argv", ["BlamixFiles.exe", "C:/some/file.txt"])
    assert not M._cli_from_app()
    parser_cmds = set()
    import argparse
    orig = argparse.ArgumentParser.parse_args

    def grab(self, args=None, namespace=None):
        for a in self._actions:
            if isinstance(a, argparse._SubParsersAction):
                parser_cmds.update(a.choices)
        raise SystemExit(0)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", grab)
    try:
        C.__dict__["main"]                                      # (patched above)
        monkeypatch.undo()
        monkeypatch.setattr(argparse.ArgumentParser, "parse_args", grab)
        with pytest.raises(SystemExit):
            C.main(["--version"])
    finally:
        monkeypatch.setattr(argparse.ArgumentParser, "parse_args", orig)
    assert parser_cmds and parser_cmds <= C.COMMANDS, parser_cmds - C.COMMANDS
