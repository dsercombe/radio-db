import re
from urllib.parse import urlparse


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value


def normalize_domain(url: str | None) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        return parsed.netloc.lower().replace("www.", "")
    except Exception:
        return ""


def normalize_country(value: str | None) -> str:
    return (value or "").strip().upper()[:8]
