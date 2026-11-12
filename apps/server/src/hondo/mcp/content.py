from __future__ import annotations

import base64
import binascii
from typing import Any

from pydantic_ai.messages import BinaryContent


def model_result(result: dict[str, Any]) -> dict[str, Any] | list[Any]:
    media: list[BinaryContent] = []
    content: list[dict[str, Any]] = []
    for part in result.get("content", []):
        if part["type"] in ("image", "audio"):
            payload, field, mime_type = part, "data", part["mimeType"]
        elif part["type"] == "resource" and "blob" in part["resource"]:
            payload, field = part["resource"], "blob"
            mime_type = payload.get("mimeType", "application/octet-stream")
        else:
            content.append(part)
            continue
        metadata = {key: value for key, value in payload.items() if key != field}
        content.append({**part, "resource": metadata} if field == "blob" else metadata)
        mime_type = mime_type.split(";", 1)[0].strip().lower()
        if mime_type == "audio/mp3":
            mime_type = "audio/mpeg"
        try:
            data = base64.b64decode(payload[field], validate=True)
            if mime_type.startswith("text/") or mime_type == "application/json":
                content.append({"type": "text", "text": data.decode("utf-8")})
            else:
                media.append(
                    BinaryContent.narrow_type(BinaryContent(data=data, media_type=mime_type))
                )
        except (binascii.Error, ValueError):
            content.append({"type": "text", "text": "The tool returned invalid media."})
    if "content" not in result:
        return result
    mapped = {**result, "content": content}
    return [mapped, *media] if media else mapped
