"""A small, in-memory BM25 baseline over source-addressable chunks."""

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from app.rag.ingestion import Chunk

TOKENIZER_VERSION = "latin_words_han_bigrams_v1"


def tokenize(text: str) -> list[str]:
    tokens = []
    for match in re.finditer(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]+", text.lower()):
        term = match.group()
        if "\u3400" <= term[0] <= "\u9fff":
            tokens.extend(term[index : index + 2] for index in range(len(term) - 1))
            if len(term) == 1:
                tokens.append(term)
        else:
            tokens.append(term)
    return tokens


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float


class BM25Index:
    """One immutable workspace snapshot; rebuild after changing documents.

    Positive smoothed IDF: log(1 + (N - df + 0.5) / (df + 0.5)).
    Score sums IDF * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl)).
    Query terms are deduplicated. Scores are rankings, not answer confidence.
    """

    def __init__(self, chunks: list[Chunk], *, k1: float = 1.2, b: float = 0.75):
        if k1 <= 0 or not 0 <= b <= 1:
            raise ValueError("BM25 requires k1 > 0 and 0 <= b <= 1")
        if len({chunk.workspace_id for chunk in chunks}) > 1:
            raise ValueError("Build a separate index for each workspace")
        self.chunks = tuple(chunks)
        self.k1, self.b = k1, b
        self.postings = defaultdict(list)
        self.lengths = []
        for index, chunk in enumerate(self.chunks):
            counts = Counter(tokenize(chunk.text))
            self.lengths.append(sum(counts.values()))
            for term, count in counts.items():
                self.postings[term].append((index, count))
        self.average_length = sum(self.lengths) / len(self.chunks) if self.chunks else 0

    def search(self, query: str, top_k: int = 5) -> list[SearchHit]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        scores = defaultdict(float)
        for term in dict.fromkeys(tokenize(query)):
            postings = self.postings.get(term, ())
            if not postings:
                continue
            idf = math.log1p((len(self.chunks) - len(postings) + 0.5) / (len(postings) + 0.5))
            for index, frequency in postings:
                norm = self.k1 * (1 - self.b + self.b * self.lengths[index] / self.average_length)
                scores[index] += idf * frequency * (self.k1 + 1) / (frequency + norm)
        ranked = sorted(
            scores,
            key=lambda index: (
                -scores[index],
                self.chunks[index].source,
                self.chunks[index].start_offset,
                self.chunks[index].chunk_id,
            ),
        )
        return [SearchHit(self.chunks[index], scores[index]) for index in ranked[:top_k]]
