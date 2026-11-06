"""An in-memory stand-in for a boto3 S3 client, shared by the storage tests."""
from __future__ import annotations

import io

from botocore.exceptions import ClientError


def _missing(op: str, code: str = "404") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "not found"}}, op)


class FakeS3:
    """Just the calls `S3Store` makes: put/get/head/delete and a paginated list."""

    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str | None] = {}
        self.calls: list[str] = []
        self.page_size = page_size
        self.corrupt_on_put = False  # store the wrong bytes (a failing upload)
        self.no_conditional_writes = False  # answer 501 to If-None-Match

    def put_object(self, *, Bucket, Key, Body, ContentType=None, IfNoneMatch=None):
        self.calls.append("put")
        if IfNoneMatch is not None:
            if self.no_conditional_writes:
                raise ClientError(
                    {"Error": {"Code": "NotImplemented", "Message": "x"},
                     "ResponseMetadata": {"HTTPStatusCode": 501}}, "PutObject")
            if Key in self.objects:
                raise ClientError(
                    {"Error": {"Code": "PreconditionFailed", "Message": "exists"},
                     "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
        data = Body if isinstance(Body, bytes) else Body.read()
        if self.corrupt_on_put:
            data = data[:-1]
        self.objects[Key] = data
        self.content_types[Key] = ContentType
        return {}

    def get_object(self, *, Bucket, Key):
        self.calls.append("get")
        if Key not in self.objects:
            raise _missing("GetObject", "NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key]), "ContentLength": len(self.objects[Key])}

    def head_object(self, *, Bucket, Key):
        self.calls.append("head")
        if Key not in self.objects:
            raise _missing("HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def delete_object(self, *, Bucket, Key):
        self.calls.append("delete")
        self.objects.pop(Key, None)
        return {}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        outer = self

        class _Paginator:
            def paginate(self, *, Bucket, Prefix=""):
                keys = sorted(k for k in outer.objects if k.startswith(Prefix))
                size = outer.page_size
                for i in range(0, max(len(keys), 1), size):
                    chunk = keys[i : i + size]
                    yield {"Contents": [{"Key": k, "Size": len(outer.objects[k])} for k in chunk]}

        return _Paginator()
