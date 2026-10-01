"""S3-compatible object storage: AWS S3, MinIO, Backblaze B2, Cloudflare R2, Wasabi,
Hetzner, DigitalOcean Spaces, …

Mapping to folders: "/" lists your buckets, "/bucket/a/b" is the prefix "a/b/" in that
bucket. S3 has no real folders: a folder exists when something is stored under it (or
an empty "name/" marker object, which "New folder" creates). Site fields used:
host = endpoint (s3.amazonaws.com, s3.eu-central-003.backblazeb2.com, …),
username = access key, password = secret key, s3_region (optional).
"""
from __future__ import annotations

import io
from datetime import timezone
from typing import BinaryIO

from ..vfs import Backend, BackendError, Cancelled, Capabilities, Entry, ProgressFn

BLOCK = 256 * 1024
MULTIPART = 64 * 1024 * 1024


class S3Backend(Backend):
    name = "s3"
    caps = Capabilities(resume=False, rename=True, chmod=False, set_mtime=False,
                        symlinks=False, atomic_replace=True)

    def __init__(self, site):
        self.site = site
        self.client = None

    # ------------------------------------------------------------ connection
    def connect(self) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise BackendError("S3 support needs the boto3 package (pip install boto3)") from None
        site = self.site
        host = site.host.strip()            # "s3.example.com", or "http://minio.lan:9000" for plain HTTP
        if "://" in host:
            endpoint, scheme = host.rstrip("/"), host.split("://", 1)[0]
        else:
            scheme = "https"
            port = f":{site.port}" if site.port and site.port != 443 else ""
            endpoint = f"https://{host}{port}"
        aws = host.endswith("amazonaws.com")
        cfg = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=15, read_timeout=60,
                     s3={"addressing_style": "virtual" if aws else "path"},
                     user_agent_extra="BlamixFiles")
        self.client = boto3.client(
            "s3", endpoint_url=None if host in ("s3.amazonaws.com", "") else endpoint,
            aws_access_key_id=site.username or None, aws_secret_access_key=site.password or None,
            region_name=getattr(site, "s3_region", "") or "us-east-1", config=cfg,
            verify=site.tls_verify if scheme == "https" else None)
        try:
            if site.remote_dir.strip("/"):
                bucket = site.remote_dir.strip("/").split("/", 1)[0]
                self.client.head_bucket(Bucket=bucket)
            else:
                self.client.list_buckets()
        except Exception as e:
            self.client = None
            raise self._error(e, site.remote_dir or "/") from None

    def close(self) -> None:
        self.client = None

    @property
    def connected(self) -> bool:
        return self.client is not None

    def home(self) -> str:
        return "/" + self.site.remote_dir.strip("/") if self.site.remote_dir.strip("/") else "/"

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _split(path: str) -> tuple[str, str]:
        p = path.strip("/")
        if not p:
            return "", ""
        bucket, _, key = p.partition("/")
        return bucket, key

    @staticmethod
    def _error(e: Exception, path: str) -> Exception:
        code = ""
        resp = getattr(e, "response", None)
        if isinstance(resp, dict):
            code = str(resp.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NoSuchBucket", "NotFound"):
            return FileNotFoundError(2, "No such file or folder", path)
        if code in ("403", "AccessDenied", "AllAccessDisabled"):
            return PermissionError(13, "Access denied", path)
        if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidToken"):
            return BackendError("Login failed: check the access key and secret key.")
        if code == "BucketAlreadyExists":
            return BackendError("That bucket name is taken (bucket names are global).")
        if e.__class__.__name__ in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError"):
            return ConnectionError(str(e))
        return BackendError(str(e))

    def _call(self, fn, path: str, **kw):
        try:
            return fn(**kw)
        except Exception as e:  # noqa: BLE001 (botocore errors -> ours)
            raise self._error(e, path) from None

    # ------------------------------------------------------------ listing
    def list(self, path: str) -> list[Entry]:
        bucket, key = self._split(path)
        if not bucket:
            r = self._call(self.client.list_buckets, path)
            return [Entry(name=b["Name"], path="/" + b["Name"], is_dir=True,
                          mtime=b["CreationDate"].replace(tzinfo=b["CreationDate"].tzinfo or timezone.utc).timestamp())
                    for b in r.get("Buckets", [])]
        prefix = key.rstrip("/") + "/" if key else ""
        out: list[Entry] = []
        token = None
        while True:
            kw = dict(Bucket=bucket, Prefix=prefix, Delimiter="/", MaxKeys=1000)
            if token:
                kw["ContinuationToken"] = token
            r = self._call(self.client.list_objects_v2, path, **kw)
            for cp in r.get("CommonPrefixes", []):
                name = cp["Prefix"][len(prefix):].rstrip("/")
                if name:
                    out.append(Entry(name=name, path=f"/{bucket}/{prefix}{name}", is_dir=True))
            for o in r.get("Contents", []):
                name = o["Key"][len(prefix):]
                if not name or name.endswith("/"):        # the folder marker itself
                    continue
                out.append(Entry(name=name, path=f"/{bucket}/{o['Key']}", size=o.get("Size", 0),
                                 mtime=o["LastModified"].timestamp()))
            if not r.get("IsTruncated"):
                break
            token = r.get("NextContinuationToken")
        return out

    def stat(self, path: str) -> Entry | None:
        bucket, key = self._split(path)
        name = self.basename(path)
        if not bucket:
            return Entry(name="/", path="/", is_dir=True)
        if not key:
            try:
                self.client.head_bucket(Bucket=bucket)
                return Entry(name=bucket, path="/" + bucket, is_dir=True)
            except Exception as e:  # noqa: BLE001
                err = self._error(e, path)
                if isinstance(err, FileNotFoundError):
                    return None
                raise err from None
        try:
            h = self.client.head_object(Bucket=bucket, Key=key)
            return Entry(name=name, path=path, size=h.get("ContentLength", 0), mtime=h["LastModified"].timestamp())
        except Exception as e:  # noqa: BLE001
            err = self._error(e, path)
            if not isinstance(err, FileNotFoundError):
                raise err from None
        r = self._call(self.client.list_objects_v2, path, Bucket=bucket, Prefix=key.rstrip("/") + "/", MaxKeys=1)
        if r.get("KeyCount", 0) or r.get("Contents"):
            return Entry(name=name, path=path, is_dir=True)
        return None

    # ------------------------------------------------------------ changes
    def mkdir(self, path: str) -> None:
        bucket, key = self._split(path)
        if not key:
            kw = {"Bucket": bucket}
            region = getattr(self.site, "s3_region", "")
            if region and region != "us-east-1":
                kw["CreateBucketConfiguration"] = {"LocationConstraint": region}
            self._call(self.client.create_bucket, path, **kw)
            return
        self._call(self.client.put_object, path, Bucket=bucket, Key=key.rstrip("/") + "/", Body=b"")

    def makedirs(self, path: str) -> None:
        bucket, key = self._split(path)
        if bucket and self.stat("/" + bucket) is None:
            self.mkdir("/" + bucket)
        if key:   # parent "folders" don't need to exist in S3; one marker is enough
            if self.stat(path) is None:
                self.mkdir(path)

    def remove(self, path: str) -> None:
        bucket, key = self._split(path)
        self._call(self.client.delete_object, path, Bucket=bucket, Key=key)

    def rmdir(self, path: str) -> None:
        bucket, key = self._split(path)
        if not key:
            self._call(self.client.delete_bucket, path, Bucket=bucket)
            return
        if self.list(path):
            raise BackendError("Folder is not empty")
        self._call(self.client.delete_object, path, Bucket=bucket, Key=key.rstrip("/") + "/")

    def remove_tree(self, path: str) -> None:
        bucket, key = self._split(path)
        if not key:
            raise BackendError("Deleting a whole bucket isn't allowed from here: empty it first, "
                               "then delete it in your provider's console.")
        prefix = key.rstrip("/") + "/"
        token = None
        while True:
            kw = dict(Bucket=bucket, Prefix=prefix, MaxKeys=1000)
            if token:
                kw["ContinuationToken"] = token
            r = self._call(self.client.list_objects_v2, path, **kw)
            keys = [{"Key": o["Key"]} for o in r.get("Contents", [])]
            if keys:
                self._call(self.client.delete_objects, path, Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
            if not r.get("IsTruncated"):
                break
            token = r.get("NextContinuationToken")

    def rename(self, src: str, dst: str) -> None:
        sb, sk = self._split(src)
        db, dk = self._split(dst)
        st = self.stat(src)
        if st is None:
            raise FileNotFoundError(2, "No such file", src)
        if st.is_dir:
            raise BackendError("Renaming folders isn't supported on S3 (it would copy every object).")
        self._call(self.client.copy_object, src, Bucket=db, Key=dk, CopySource={"Bucket": sb, "Key": sk})
        self._call(self.client.delete_object, src, Bucket=sb, Key=sk)

    def checksum(self, path, algos=("sha256", "md5")):
        """S3's ETag is the MD5 of the content for objects uploaded in one part (not for
        multipart uploads or SSE-KMS); anything else: None."""
        if "md5" not in algos:
            return None
        bucket, key = self._split(path)
        try:
            etag = self.client.head_object(Bucket=bucket, Key=key).get("ETag", "").strip('"')
        except Exception:  # noqa: BLE001
            return None
        if len(etag) == 32 and "-" not in etag:
            return "md5", etag.lower()
        return None

    # ------------------------------------------------------------ data
    def download(self, path: str, fp: BinaryIO, offset: int = 0,
                 progress: ProgressFn | None = None) -> None:
        bucket, key = self._split(path)
        kw = dict(Bucket=bucket, Key=key)
        if offset:
            kw["Range"] = f"bytes={offset}-"
        r = self._call(self.client.get_object, path, **kw)
        body = r["Body"]
        try:
            while chunk := body.read(BLOCK):
                fp.write(chunk)
                if progress:
                    progress(len(chunk))
        finally:
            body.close()

    def upload(self, fp: BinaryIO, path: str, offset: int = 0,
               progress: ProgressFn | None = None) -> None:
        if offset:
            raise BackendError("S3 can't resume uploads")
        from boto3.s3.transfer import TransferConfig
        bucket, key = self._split(path)
        if not key:
            raise BackendError("Choose a bucket first: files can't be stored at the top level")
        cfg = TransferConfig(multipart_threshold=MULTIPART, multipart_chunksize=16 * 1024 * 1024,
                             max_concurrency=4, use_threads=True)
        try:
            self.client.upload_fileobj(fp, bucket, key, Config=cfg,
                                       Callback=(lambda n: progress(n)) if progress else None)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            inner = e.__cause__ or e.__context__
            if isinstance(inner, Cancelled):
                raise inner from None
            raise self._error(e, path) from None

    def write_bytes(self, path: str, data: bytes, atomic: bool = True) -> None:
        # a PUT replaces the object in one step: no temp file needed
        self.upload(io.BytesIO(data), path)
