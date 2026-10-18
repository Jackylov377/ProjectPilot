"""Compare retrieval on a frozen small corpus; model calls are explicit options."""

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app.rag.ingestion import chunk_document, parse_document
from app.rag.rerank import RerankClient
from app.rag.retrieval import TOKENIZER_VERSION, BM25Index
from app.rag.vector import EmbeddingClient, HybridIndex, VectorIndex


def _key(value):
    return value["source"], value["start_line"], value["end_line"], value.get("page")


def _summarize(cases):
    answerable = [case for case in cases if case["relevant"]]
    unanswerable = [case for case in cases if not case["relevant"]]
    return {
        "queries": len(cases),
        "answerable_queries": len(answerable),
        "no_answer_queries": len(unanswerable),
        "mean_recall_at_k": sum(case["recall_at_k"] for case in answerable) / len(answerable)
        if answerable
        else None,
        "mrr_at_k": sum(case["reciprocal_rank_at_k"] for case in answerable) / len(answerable)
        if answerable
        else None,
        "no_answer_empty_result_rate": sum(not case["results"] for case in unanswerable)
        / len(unanswerable)
        if unanswerable
        else None,
        "no_answer_nonempty_count": sum(bool(case["results"]) for case in unanswerable),
    }


def _grade(query, hits):
    relevant = {_key(label) for label in query["relevant"]}
    results = [
        {"score": hit.score, **asdict(hit.chunk), "citation": hit.chunk.citation} for hit in hits
    ]
    ranks = [rank for rank, hit in enumerate(results, 1) if _key(hit) in relevant]
    return {
        **query,
        "recall_at_k": len(ranks) / len(relevant) if relevant else None,
        "reciprocal_rank_at_k": 1 / ranks[0] if ranks else (0 if relevant else None),
        "results": results,
    }


def _metrics(cases):
    return {
        "summary": _summarize(cases),
        "splits": {
            split: _summarize([case for case in cases if case["split"] == split])
            for split in sorted({case["split"] for case in cases})
        },
        "cases": cases,
    }


def compare_chunking(dataset_path: Path, top_k: int = 3) -> dict:
    """Keep original source evidence fixed rather than relabel each chunk layout."""
    raw = dataset_path.read_bytes()
    dataset = json.loads(raw)
    workspace = dataset_path.parent / dataset["corpus_dir"]
    documents = {source: parse_document(workspace, source) for source in dataset["documents"]}
    for source, document in documents.items():
        if document.version != dataset["documents"][source]:
            raise ValueError(f"Frozen corpus changed: {source}")
    evidence = {}
    for query in dataset["queries"]:
        spans = []
        for label in query["relevant"]:
            lines = documents[label["source"]].text.splitlines(keepends=True)
            start = sum(map(len, lines[: label["start_line"] - 1]))
            end = sum(map(len, lines[: label["end_line"]]))
            spans.append({"source": label["source"], "start_offset": start, "end_offset": end})
        evidence[query["case_id"]] = spans
    comparisons = []
    for size, overlap in ((160, 0), (160, 40), (320, 0), (320, 80), (1000, 0), (1000, 200)):
        chunks = [
            chunk
            for document in documents.values()
            for chunk in chunk_document(document, size, overlap)
        ]
        index, cases = BM25Index(chunks), []
        for query in dataset["queries"]:
            hits = index.search(query["query"], top_k)
            gold = evidence[query["case_id"]]
            covered = sum(
                any(
                    hit.chunk.source == span["source"]
                    and hit.chunk.start_offset <= span["start_offset"]
                    and hit.chunk.end_offset >= span["end_offset"]
                    for hit in hits
                )
                for span in gold
            )
            positions = {
                (hit.chunk.source, offset)
                for hit in hits
                for offset in range(hit.chunk.start_offset, hit.chunk.end_offset)
            }
            chars = sum(len(hit.chunk.text) for hit in hits)
            cases.append(
                {
                    "case_id": query["case_id"],
                    "evidence_coverage": covered / len(gold) if gold else None,
                    "context_chars": chars,
                    "repeated_chars": chars - len(positions),
                    "results": [asdict(hit.chunk) for hit in hits],
                }
            )
        answerable = [case for case in cases if case["evidence_coverage"] is not None]
        comparisons.append(
            {
                "max_chars": size,
                "overlap_chars": overlap,
                "chunk_count": len(chunks),
                "mean_evidence_coverage": sum(case["evidence_coverage"] for case in answerable)
                / len(answerable),
                "mean_context_chars": sum(case["context_chars"] for case in cases) / len(cases),
                "mean_repeated_chars": sum(case["repeated_chars"] for case in cases) / len(cases),
                "cases": cases,
            }
        )
    return {
        "dataset_sha256": sha256(raw).hexdigest(),
        "corpus_versions": dataset["documents"],
        "top_k": top_k,
        "model_requests": 0,
        "grading": "An original labelled source span must fit fully inside a returned chunk. No-answer cases are not included in evidence coverage.",
        "evidence": evidence,
        "comparisons": comparisons,
    }


def evaluate(
    dataset_path: Path,
    top_k: int = 3,
    *,
    embedder: EmbeddingClient | None = None,
    embedding_snapshot: Path | None = None,
    reuse_embeddings: Path | None = None,
    reranker: RerankClient | None = None,
    candidate_k: int = 10,
) -> dict:
    if embedder and reuse_embeddings:
        raise ValueError("Generate new embeddings or reuse a snapshot, not both")
    if top_k < 1 or (reranker and candidate_k < top_k):
        raise ValueError("Use positive top_k and at least as many rerank candidates")
    raw = dataset_path.read_bytes()
    dataset = json.loads(raw)
    workspace = dataset_path.parent / dataset["corpus_dir"]
    chunks = []
    for source, expected_version in dataset["documents"].items():
        document = parse_document(workspace, source)
        if document.version != expected_version:
            raise ValueError(f"Frozen corpus changed: {source}")
        chunks.extend(chunk_document(document, dataset["max_chars"]))
    labels = {_key(asdict(chunk)) for chunk in chunks}
    if len(labels) != len(chunks):
        raise ValueError(
            "Baseline needs unique source/line ranges; use character labels for split lines"
        )
    index = BM25Index(chunks)
    dense = hybrid = None
    dense_cases, hybrid_cases, reranked_cases, candidate_cases = [], [], [], []
    embedding_elapsed_seconds = 0
    embedding_metadata = None
    if reuse_embeddings:
        snapshot = json.loads(reuse_embeddings.read_text(encoding="utf-8"))
        if (
            snapshot["dataset_sha256"] != sha256(raw).hexdigest()
            or snapshot["chunk_ids"] != [chunk.chunk_id for chunk in chunks]
            or snapshot["query_case_ids"] != [query["case_id"] for query in dataset["queries"]]
        ):
            raise ValueError("Embedding snapshot does not match this dataset and workspace")
        document_vectors, query_vectors = snapshot["document_vectors"], snapshot["query_vectors"]
        if len(query_vectors) != len(dataset["queries"]):
            raise ValueError("Embedding snapshot is missing query vectors")
        embedding_metadata = snapshot["embedding"]
    if embedder:
        embedding_started = time.perf_counter()
        document_vectors = embedder.embed([chunk.text for chunk in chunks])
        query_vectors = embedder.embed([query["query"] for query in dataset["queries"]])
        embedding_elapsed_seconds = time.perf_counter() - embedding_started
        embedding_metadata = embedder.metadata()
        if embedding_snapshot:
            embedding_snapshot.parent.mkdir(parents=True, exist_ok=True)
            embedding_snapshot.write_text(
                json.dumps(
                    {
                        "dataset_sha256": sha256(raw).hexdigest(),
                        "embedding": embedder.metadata(),
                        "chunk_ids": [chunk.chunk_id for chunk in chunks],
                        "document_vectors": document_vectors,
                        "query_case_ids": [query["case_id"] for query in dataset["queries"]],
                        "query_vectors": query_vectors,
                    }
                ),
                encoding="utf-8",
            )
    if embedding_metadata:
        dense = VectorIndex(chunks, document_vectors)
        hybrid = HybridIndex(index, dense)
    cases = []
    started = time.perf_counter()
    for number, query in enumerate(dataset["queries"]):
        relevant = {_key(label) for label in query["relevant"]}
        if relevant - labels:
            raise ValueError(f"Gold labels do not match frozen chunks: {query['case_id']}")
        cases.append(_grade(query, index.search(query["query"], top_k)))
        if dense:
            dense_cases.append(_grade(query, dense.search(query_vectors[number], top_k)))
            hybrid_cases.append(
                _grade(query, hybrid.search(query["query"], query_vectors[number], top_k))
            )
        if reranker:
            candidates = (
                hybrid.search(query["query"], query_vectors[number], candidate_k)
                if hybrid
                else index.search(query["query"], candidate_k)
            )
            candidate_cases.append(_grade(query, candidates))
            reranked_cases.append(_grade(query, reranker.rerank(query["query"], candidates, top_k)))
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_version": dataset["dataset_version"],
        "dataset_sha256": sha256(raw).hexdigest(),
        "corpus_versions": dataset["documents"],
        "configuration": {
            "retriever": "bm25",
            "k1": index.k1,
            "b": index.b,
            "tokenizer": TOKENIZER_VERSION,
            "max_chars": dataset["max_chars"],
            "overlap": 0,
            "top_k": top_k,
        },
        "chunk_count": len(chunks),
        "query_elapsed_seconds": time.perf_counter() - started,
        "model_requests": (len(embedder.requests) if embedder else 0)
        + (len(reranker.requests) if reranker else 0),
        "embedding_requests_this_run": len(embedder.requests) if embedder else 0,
        "rerank_requests_this_run": len(reranker.requests) if reranker else 0,
        "generation_requests": 0,
        **_metrics(cases),
    }
    if embedding_metadata:
        report["embedding"] = embedding_metadata
        report["embedding_source"] = "snapshot" if reuse_embeddings else "api"
        if reuse_embeddings:
            report["embedding_snapshot_sha256"] = sha256(reuse_embeddings.read_bytes()).hexdigest()
        report["embedding_elapsed_seconds"] = embedding_elapsed_seconds
        report["comparisons"] = {
            "vector": {"similarity": "cosine", **_metrics(dense_cases)},
            "hybrid": {
                "fusion": "rrf",
                "rank_constant": hybrid.rank_constant,
                "candidate_k": hybrid.candidate_k,
                **_metrics(hybrid_cases),
            },
        }
    if reranker:
        report["reranking"] = reranker.metadata()
        report.setdefault("comparisons", {})["reranked"] = {
            "base_retriever": "hybrid" if hybrid else "bm25",
            "candidate_k": candidate_k,
            "candidates": candidate_cases,
            **_metrics(reranked_cases),
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="运行固定语料的 BM25 开发基线")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--output", type=Path, help="结果 JSON，省略时输出到标准输出")
    parser.add_argument(
        "--compare-chunking",
        action="store_true",
        help="离线比较六组长度/重叠配置，按固定原文依据评分",
    )
    parser.add_argument(
        "--compare-vector", action="store_true", help="调用已配置 Embedding，比较向量和 Hybrid"
    )
    parser.add_argument("--embedding-model", default="text-embedding-v4")
    parser.add_argument("--embedding-dimensions", type=int, default=256)
    parser.add_argument("--embedding-snapshot", type=Path, help="保存本次向量，便于离线核对排名")
    parser.add_argument(
        "--reuse-embeddings", type=Path, help="复用保存的向量，不再次请求 Embedding"
    )
    parser.add_argument(
        "--rerank", action="store_true", help="调用重排接口，比较候选精排前后的结果"
    )
    parser.add_argument("--rerank-model", default="gte-rerank-v2")
    parser.add_argument("--candidate-k", type=int, default=10)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    if args.compare_vector and args.reuse_embeddings:
        parser.error("--compare-vector 和 --reuse-embeddings 只能选择一种")
    if args.compare_chunking and (args.compare_vector or args.reuse_embeddings or args.rerank):
        parser.error("切片对照单独运行，不同时请求向量或重排服务")
    try:
        embedder = reranker = None
        if args.compare_vector or args.rerank:
            load_dotenv(args.env_file, override=False)
        if args.compare_vector:
            embedder = EmbeddingClient(
                os.getenv("PROJECTPILOT_API_BASE", ""),
                os.getenv("PROJECTPILOT_API_KEY", ""),
                args.embedding_model,
                args.embedding_dimensions,
            )
        if args.rerank:
            reranker = RerankClient(
                os.getenv("PROJECTPILOT_API_BASE", ""),
                os.getenv("PROJECTPILOT_API_KEY", ""),
                args.rerank_model,
            )
        report = (
            compare_chunking(args.dataset, args.top_k)
            if args.compare_chunking
            else evaluate(
                args.dataset,
                args.top_k,
                embedder=embedder,
                embedding_snapshot=args.embedding_snapshot,
                reuse_embeddings=args.reuse_embeddings,
                reranker=reranker,
                candidate_k=args.candidate_k,
            )
        )
        content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
        else:
            sys.stdout.buffer.write(content.encode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    except httpx.HTTPError as exc:
        parser.exit(1, f"Retrieval service request failed: {type(exc).__name__}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
