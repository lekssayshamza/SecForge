"""Canonical tags and image name for a persisted room-template capture.

Initial creation and recovery use identical tags. EC2 CreateImage has no
supported ClientToken; tag lookup identifies an existing image but a miss does
not prove that creation never happened. Recovery must not re-issue creation.
The legacy token helper remains for internal caller compatibility only.
"""

from typing import Optional

CAPTURE_CLIENT_TOKEN_PREFIX = "secforge-tmpl-"


def capture_client_token(template_id: int) -> str:
    """Return a legacy internal capture label; never an AWS idempotency token."""
    return f"{CAPTURE_CLIENT_TOKEN_PREFIX}{template_id}"


def capture_image_name(room_id: int, version: int) -> str:
    """Return the AMI name used for a room-template capture."""
    return f"secforge-room-{room_id}-v{version}"


def capture_image_tags(
    *,
    room_id: int,
    template_id: int,
    version: int,
    builder_id: Optional[int],
    capture_id: Optional[int],
) -> dict:
    """Return the exact tag set for a room-template capture.

    Both the initial capture and recovery build tags through this function so a
    recovery lookup matches the original request.
    ``builder_id`` and ``capture_id`` are included only when known so tag-based
    lookup can still match a superset of tags for a partially-known record.
    """
    tags = {
        "Project": "SecForge",
        "ManagedBy": "SecForge",
        "ResourceType": "ami",
        "RoomId": str(room_id),
        "RoomTemplateId": str(template_id),
        "Version": str(version),
    }
    if builder_id is not None:
        tags["SourceBuilderId"] = str(builder_id)
    if capture_id is not None:
        tags["CaptureRequestId"] = str(capture_id)
    return tags
