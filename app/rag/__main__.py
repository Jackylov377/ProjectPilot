"""Preview chunks or search an in-memory index over explicitly selected files."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from app.rag.ingestion import chunk_document, parse_document
from app.rag.retrieval import TOKENIZER_VERSION, BM25Index


def main() -> int:
    parser = argparse.ArgumentParser(description="解析 Markdown/TXT/PDF，预览片段或执行 BM25 检索")
    parser.add_argument("paths", nargs="+", help="工作区内的相对文件路径")
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--max-chars", type=int, default=1000)
    parser.add_argument("--overlap-chars", type=int, default=0)
    parser.add_argument("--query", help="提供时执行检索，省略时预览 JSONL 片段")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    if args.max_chars < 1:
        parser.error("--max-chars must be positive")
    if args.top_k < 1:
        parser.error("--top-k must be positive")
    try:
        chunks = []
        for path in args.paths:
            document = parse_document(args.workspace, path)
            if document.pages_without_text:
                print(
                    f"{document.source}: no text extracted on PDF pages {document.pages_without_text}; no OCR performed",
                    file=sys.stderr,
                )
            for chunk in chunk_document(document, args.max_chars, args.overlap_chars):
                if args.query is None:
                    emit({**asdict(chunk), "citation": chunk.citation})
                else:
                    chunks.append(chunk)
        if args.query is not None:
            hits = BM25Index(chunks).search(args.query, args.top_k)
            emit(
                {
                    "query": args.query,
                    "retriever": "bm25",
                    "tokenizer": TOKENIZER_VERSION,
                    "max_chars": args.max_chars,
                    "overlap_chars": args.overlap_chars,
                    "top_k": args.top_k,
                    "results": [
                        {"score": hit.score, **asdict(hit.chunk), "citation": hit.chunk.citation}
                        for hit in hits
                    ],
                }
            )
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    return 0


def emit(value):
    sys.stdout.buffer.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
