"""The database is one SQLite file kept in S3, next to the PDFs.

Each Lambda instance keeps a copy in /tmp keyed by ETag and writes back with
If-Match, so a save that raced another one fails instead of overwriting it.
Without a bucket (local dev) the same calls work against a directory.
"""
import contextlib
import os
import shutil

from botocore.exceptions import ClientError

import db


class Conflict(Exception):
    """Another save landed first; this change was not stored."""


def _status(exc: ClientError) -> int:
    return exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)


class Store:
    def __init__(self, root: str, s3=None, bucket: str | None = None, db_key: str = "db/vaad.sqlite"):
        os.makedirs(root, exist_ok=True)
        self.root = root
        self.s3 = s3
        self.bucket = bucket
        self.db_key = db_key
        self.path = os.path.join(root, "vaad.sqlite")
        self.etag: str | None = None

    @contextlib.contextmanager
    def read(self):
        self._refresh()
        conn = db.connect(self.path)
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def write(self):
        self._refresh()
        conn = db.connect(self.path)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._upload()

    def location(self, key: str) -> str:
        """Where a stored file lives, in a form someone can look it up by."""
        return f"s3://{self.bucket}/{key}" if self.s3 else os.path.abspath(os.path.join(self.root, key))

    def put_file(self, key: str, body: bytes, content_type: str) -> None:
        if self.s3 is None:
            path = os.path.join(self.root, key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(body)
            return
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)

    def get_file(self, key: str) -> bytes | None:
        if self.s3 is None:
            path = os.path.join(self.root, key)
            if not os.path.exists(path):
                return None
            with open(path, "rb") as f:
                return f.read()
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if _status(exc) == 404:
                return None
            raise

    def _refresh(self) -> None:
        if self.s3 is None:
            return
        request = {"Bucket": self.bucket, "Key": self.db_key}
        if self.etag and os.path.exists(self.path):
            request["IfNoneMatch"] = self.etag
        try:
            obj = self.s3.get_object(**request)
        except ClientError as exc:
            if _status(exc) == 304:
                return
            if _status(exc) == 404:
                # First run: start from an empty database, created on the first save.
                self._discard()
                return
            raise
        part = self.path + ".part"
        with open(part, "wb") as f:
            shutil.copyfileobj(obj["Body"], f)
        os.replace(part, self.path)
        self.etag = obj["ETag"]

    def _upload(self) -> None:
        if self.s3 is None:
            return
        condition = {"IfMatch": self.etag} if self.etag else {"IfNoneMatch": "*"}
        try:
            with open(self.path, "rb") as f:
                response = self.s3.put_object(
                    Bucket=self.bucket, Key=self.db_key, Body=f,
                    ContentType="application/vnd.sqlite3", **condition,
                )
        except ClientError as exc:
            # The local copy now holds a commit S3 never got; drop it so the next
            # request starts again from what is actually stored.
            self._discard()
            if _status(exc) in (409, 412):
                raise Conflict() from exc
            raise
        self.etag = response["ETag"]

    def _discard(self) -> None:
        self.etag = None
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.path)
