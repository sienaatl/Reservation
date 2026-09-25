"""Restrict post-login navigation to local application paths."""

from urllib.parse import unquote, urlsplit, urlunsplit


def safe_login_destination(value, public_base_url="", default="/admin"):
    if not value or value.startswith("//") or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return default
    try:
        target = urlsplit(value)
        if target.scheme or target.netloc:
            origin = urlsplit(public_base_url)
            if (target.scheme != "https" or not origin.netloc
                    or target.netloc.lower() != origin.netloc.lower()
                    or target.username is not None):
                return default
        path = target.path
        # Browsers and proxies can interpret encoded slashes/backslashes
        # differently. Reject ambiguous paths instead of normalizing them.
        decoded = unquote(path)
        if (not path.startswith("/") or path.startswith("//")
                or not decoded.startswith("/") or decoded.startswith("//")
                or "\\" in decoded or "%" in decoded
                or any(ord(c) < 32 or ord(c) == 127 for c in unquote(value))):
            return default
        return urlunsplit(("", "", path, target.query, ""))
    except ValueError:
        return default
