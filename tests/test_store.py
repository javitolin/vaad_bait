import hashlib
import io
import tempfile
import unittest

from botocore.exceptions import ClientError

from store import Conflict, Store


def _error(status: int, op: str) -> ClientError:
    return ClientError({"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}}, op)


class FakeS3:
    """Just enough of S3's conditional reads and writes."""

    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key, IfNoneMatch=None):
        if Key not in self.objects:
            raise _error(404, "GetObject")
        etag, body = self.objects[Key]
        if IfNoneMatch == etag:
            raise _error(304, "GetObject")
        return {"ETag": etag, "Body": io.BytesIO(body)}

    def put_object(self, Bucket, Key, Body, ContentType=None, IfMatch=None, IfNoneMatch=None):
        current = self.objects.get(Key)
        if (IfNoneMatch == "*" and current) or (IfMatch is not None and (not current or current[0] != IfMatch)):
            raise _error(412, "PutObject")
        data = Body.read() if hasattr(Body, "read") else Body
        etag = f'"{hashlib.md5(data).hexdigest()}"'
        self.objects[Key] = (etag, data)
        return {"ETag": etag}


def owner(store: Store, apt: int) -> str:
    with store.read() as conn:
        return conn.execute("SELECT owner FROM apartments WHERE id = ?", (apt,)).fetchone()[0]


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.dirs = [tempfile.TemporaryDirectory() for _ in range(2)]
        # Two Lambda instances sharing one bucket.
        self.a, self.b = (Store(d.name, self.s3, "bucket") for d in self.dirs)

    def tearDown(self):
        for d in self.dirs:
            d.cleanup()

    def test_stale_write_is_rejected_and_nothing_is_lost(self):
        with self.a.write() as conn:
            conn.execute("UPDATE apartments SET owner = 'a1' WHERE id = 1")
        self.assertEqual(owner(self.b, 1), "a1")

        with self.assertRaises(Conflict):
            with self.b.write() as conn:
                conn.execute("UPDATE apartments SET owner = 'b' WHERE id = 2")
                # a saves while b's request is still running.
                with self.a.write() as other:
                    other.execute("UPDATE apartments SET owner = 'a2' WHERE id = 1")

        self.assertEqual((owner(self.b, 1), owner(self.b, 2)), ("a2", ""))
        with self.b.write() as conn:
            conn.execute("UPDATE apartments SET owner = 'b' WHERE id = 2")
        self.assertEqual((owner(self.a, 1), owner(self.a, 2)), ("a2", "b"))

    def test_first_save_refuses_to_overwrite_an_existing_database(self):
        with self.assertRaises(Conflict):
            with self.b.write() as conn:
                conn.execute("UPDATE apartments SET owner = 'b' WHERE id = 1")
                with self.a.write() as other:
                    other.execute("UPDATE apartments SET owner = 'a' WHERE id = 1")
        self.assertEqual(owner(self.b, 1), "a")


if __name__ == "__main__":
    unittest.main()
