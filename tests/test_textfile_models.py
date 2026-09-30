import pytest

from blamixfiles.core.textfile import NotText, decode, encode
from blamixfiles.models import Site, open_or_create
from blamixfiles.vault import WrongPassword


@pytest.mark.parametrize("raw", [
    b"server {\n  listen 80;\n}\n",
    b"line1\r\nline2\r\n",
    b"\xef\xbb\xbfBOM file\r\nwith CRLF",
    "café cp1252\n".encode("cp1252"),
    "unicode éè ✓\n".encode("utf-8"),
    "utf16\r\n".encode("utf-16"),
    b"no newline at end",
])
def test_decode_encode_roundtrip(raw):
    text, meta = decode(raw)
    assert "\r" not in text
    assert encode(text, meta) == raw


def test_crlf_kept_after_edit():
    text, meta = decode(b"a\r\nb\r\n")
    assert meta.eol == "\r\n"
    assert encode(text + "c\n", meta) == b"a\r\nb\r\nc\r\n"


def test_binary_refused():
    with pytest.raises(NotText):
        decode(b"\x7fELF\x00\x00binary")


def test_site_from_url():
    s, path = Site.from_url("ftps://bob:s%40cret@example.com:2121/var/www")
    assert (s.protocol, s.host, s.port, s.username, s.password, path) == (
        "ftps", "example.com", 2121, "bob", "s@cret", "/var/www")
    s, _ = Site.from_url("deploy@10.0.0.5")
    assert s.protocol == "sftp" and s.username == "deploy" and s.effective_port == 22
    s, _ = Site.from_url("ftp://files.example.org")
    assert s.auth == "anonymous"
    with pytest.raises(ValueError):
        Site.from_url("gopher://x")


def test_vault_store(tmp_path):
    p = tmp_path / "vault.bfv"
    store = open_or_create(p, "master", create=True)
    store.upsert(Site(name="prod", protocol="sftp", host="h", username="u", password="secret"))
    assert b"secret" not in p.read_bytes()
    again = open_or_create(p, "master", create=False)
    assert again.find("PROD").password == "secret"
    with pytest.raises(WrongPassword):
        open_or_create(p, "nope", create=False)


FZ_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FileZilla3 version="3.66.0">
  <Servers>
    <Server>
      <Host>ftp.example.com</Host><Port>21</Port><Protocol>4</Protocol><Type>0</Type>
      <User>web</User><Pass encoding="base64">czNjcmV0IQ==</Pass><Logontype>1</Logontype>
      <PasvMode>MODE_DEFAULT</PasvMode><Name>Shop FTP</Name>
      <RemoteDir>1 0 3 var 3 www 8 my files</RemoteDir>
    </Server>
    <Folder expanded="1">Clients
      <Folder>ACME
        <Server>
          <Host>10.0.0.9</Host><Port>2222</Port><Protocol>1</Protocol><User>deploy</User>
          <Logontype>5</Logontype><Keyfile>C:\\keys\\id_ed25519</Keyfile><Name>acme-prod</Name>
        </Server>
      </Folder>
    </Folder>
    <Server><Host>s3.amazonaws.com</Host><Protocol>7</Protocol><Name>bucket</Name></Server>
  </Servers>
</FileZilla3>"""


def test_import_filezilla(tmp_path):
    from blamixfiles.importers import import_filezilla
    p = tmp_path / "sitemanager.xml"
    p.write_text(FZ_XML, encoding="utf-8")
    sites = {s.name: s for s in import_filezilla(p)}
    assert set(sites) == {"Shop FTP", "acme-prod"}          # S3 skipped for now
    ftp = sites["Shop FTP"]
    assert (ftp.protocol, ftp.password, ftp.port, ftp.remote_dir) == ("ftps", "s3cret!", 0, "/var/www/my files")
    ssh = sites["acme-prod"]
    assert (ssh.protocol, ssh.port, ssh.auth, ssh.key_path, ssh.group) == (
        "sftp", 2222, "key", "C:\\keys\\id_ed25519", "Clients/ACME")
