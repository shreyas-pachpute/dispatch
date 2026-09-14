"""Retrieval over the knowledge base with citations. v0 is lexical (BM25-style) over a JSON corpus;
Phase 1 replaces it with Postgres full text + pgvector, same interface."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

WORD = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> list[str]:
    return WORD.findall(s.lower())


class Knowledge:
    def __init__(self, path: Path):
        self.chunks = json.loads(path.read_text(encoding="utf8"))
        self.docs = [_tokens(c["title"] + " " + c["text"]) for c in self.chunks]
        self.avgdl = sum(len(d) for d in self.docs) / max(1, len(self.docs))
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 3) -> list[dict]:
        q = _tokens(query)
        scored = []
        for i, d in enumerate(self.docs):
            tf = Counter(d)
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf.get(t, 0) * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * len(d) / self.avgdl))
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        return [{**self.chunks[i], "score": round(s, 2)} for s, i in scored[:k]]

    def by_id(self, cid: str) -> dict | None:
        return next((c for c in self.chunks if c["id"] == cid), None)
