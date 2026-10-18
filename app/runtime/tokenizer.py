"""Fail fast on a missing tokenizer cache, instead of downloading inside the agent loop."""

import hashlib
import os
from pathlib import Path

URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"


def require_tokenizer_cache() -> None:
    cache = Path(os.environ.get("TIKTOKEN_CACHE_DIR") or Path.cwd() / ".cache" / "tiktoken")
    target = cache / hashlib.sha1(URL.encode()).hexdigest()
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError("分词缓存缺失或损坏，请先运行 python scripts/prepare_tokenizer.py")
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache.resolve())
