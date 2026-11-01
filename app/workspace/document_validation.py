"""Bounded-process upload validation, without app startup or external services."""

import json
from pathlib import Path
import sys

from app.rag.ingestion import parse_document


def main():
    try:
        root, name, result = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
        document = parse_document(root, name)
        if not document.text.strip():
            raise ValueError("Document has no text")
        value = dict(text=document.text[:20000], truncated=len(document.text) > 20000,
                     pages_without_text=document.pages_without_text, page_count=len(document.pages))
        result.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return 0
    except Exception:
        return 1  # Never echo private document contents or parser diagnostics.


if __name__ == "__main__":
    raise SystemExit(main())
