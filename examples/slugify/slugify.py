import re


def slugify(text: str) -> str:
    """Lowercase, replace every run of non-alphanumerics with one hyphen, trim."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")
