"""Logical source identities, separate from project and snapshot identities."""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit


def local_identity(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def github_identity(url: str) -> tuple[str, str]:
    parsed = urlsplit(url.strip())
    parts = parsed.path.rstrip("/").split("/")
    if (
        parsed.scheme != "https" or parsed.netloc.casefold() != "github.com"
        or parsed.query or parsed.fragment or len(parts) != 3
        or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", parts[1])
    ):
        raise ValueError("Expected a public GitHub repository homepage URL")
    owner, repo = parts[1].casefold(), parts[2].casefold().removesuffix(".git")
    if not re.fullmatch(r"[a-z0-9_.-]{1,100}", repo) or repo in {".", ".."}:
        raise ValueError("Invalid GitHub repository name")
    return f"github:{owner}/{repo}", f"https://github.com/{owner}/{repo}"
