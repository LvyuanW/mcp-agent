"""Inline media projection for providers, with no local workspace access."""

import base64
import binascii
from copy import deepcopy

MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ImageRequestError(ValueError):
    pass


def image_bytes(block: dict) -> bytes:
    encoded = block.get("data") or block.get("base64")
    if not isinstance(encoded, str):
        raise ImageRequestError("Remote media must include inline base64 data")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ImageRequestError("Remote media contains invalid base64 data") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageRequestError("Remote media exceeds the 10 MiB request limit")
    return data


def project_images(messages: list[dict]) -> list[dict]:
    return deepcopy(messages)
