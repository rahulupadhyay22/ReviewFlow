"""Cloudflare R2 blob storage (SAD.md §"Foundational Decisions": R2 holds
only blobs -- QR images, CSV imports, exports -- never queryable business
records; Security-Controls.md §File / Object Storage).

Thin S3-compatible client wrapper. Never logs a key's contents; a caller
that logs failures logs the object key and the exception class only, never
the exception message (which can embed request/response detail).
"""
from functools import lru_cache

import boto3
from django.conf import settings


@lru_cache(maxsize=1)
def _client():
    return boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT_URL,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        # R2 has no regions; Cloudflare's docs specify "auto" for the
        # S3-compatible API's required region parameter.
        region_name="auto",
    )


def put_object(key: str, data: bytes) -> None:
    _client().put_object(Bucket=settings.R2_BUCKET, Key=key, Body=data)


def get_object(key: str) -> bytes:
    return _client().get_object(Bucket=settings.R2_BUCKET, Key=key)["Body"].read()


def delete_object(key: str) -> None:
    _client().delete_object(Bucket=settings.R2_BUCKET, Key=key)
