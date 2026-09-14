"""Code under test for the worked example: a slug function with three rules.

The example suite pins lowercasing and edge trimming but not the collapse of
separator runs, so the collapse_dropped mutant survives on purpose.
"""

import re


def slugify(text: str) -> str:
    """Lowercase, replace every run of non-alphanumerics with one hyphen, trim."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")
