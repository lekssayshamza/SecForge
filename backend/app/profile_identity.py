"""Small public profile values; no credentials or infrastructure identity here."""
import re

AVATAR_STYLES = ("initials", "terminal", "compass", "code", "shield", "network")
AVATAR_COLORS = ("red", "navy", "green", "blue", "purple")


def normalize_username(value: str) -> str:
    if not isinstance(value, str) or not value.strip().isascii():
        raise ValueError("Use an ASCII username.")
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,49}", value):
        raise ValueError("Use 3–50 letters, numbers, dots, underscores or hyphens; start with a letter or number.")
    return value


def vpn_profile_filename(username: str) -> str:
    # Also handles legacy names without changing their certificate identity.
    name = re.sub(r"[^a-z0-9._-]", "-", username.lower()).strip("._-")[:50] or "user"
    return f"secforge-{name}.ovpn"
