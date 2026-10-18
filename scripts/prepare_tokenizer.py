"""Prepare the verified tokenizer cache on Windows, macOS or Linux."""

import hashlib
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
ROOT = Path(__file__).resolve().parents[1]


def main():
    cache = Path(os.environ.get("TIKTOKEN_CACHE_DIR") or ROOT / ".cache/tiktoken")
    target = cache / hashlib.sha1(URL.encode()).hexdigest()
    temporary = None
    try:
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == SHA256:
            print("Tokenizer cache is ready.")
            return 0
        cache.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(URL, timeout=60) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if hashlib.sha256(raw).hexdigest() != SHA256:
            raise ValueError("Tokenizer download failed SHA-256 verification.")
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
        temporary.replace(target)
        print("Tokenizer cache prepared and SHA-256 verified.")
        return 0
    except (OSError, ValueError, urllib.error.URLError) as exc:
        # Avoid printing proxy credentials or other local connection configuration.
        print(f"Tokenizer preparation failed ({type(exc).__name__}). Check network and retry.")
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
