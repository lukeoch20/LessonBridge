"""Search / retrieval layer.

A SQLite FTS5 virtual table over parsed document text. No vector database is
needed for the MVP; chunked full-text search answers the questions the design
document lists (pacing-guide expectations, related standards, non-instructional
dates, assessment proximity).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150


def ensure_fts_schema(engine: Engine) -> None:
    if not engine.url.get_backend_name().startswith("sqlite"):
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks USING fts5("
                "document_id UNINDEXED, version_id UNINDEXED, doc_type UNINDEXED, scope UNINDEXED, "
                "chunk_no UNINDEXED, title, body, tokenize='porter unicode61')"
            )
        )


def chunk_text(body: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    body = re.sub(r"[ \t]+", " ", body or "").strip()
    if not body:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(body):
        end = min(len(body), start + size)
        # Prefer to break on a paragraph or sentence boundary.
        if end < len(body):
            window = body[start:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > size // 2:
                end = start + cut + 1
        chunks.append(body[start:end].strip())
        if end >= len(body):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def index_version(session: Session, *, document_id: int, version_id: int, doc_type: str, scope: str, title: str, body: str) -> int:
    """(Re)index one document version. Returns the number of chunks written."""
    session.execute(text("DELETE FROM document_chunks WHERE version_id = :vid"), {"vid": version_id})
    chunks = chunk_text(body)
    for i, chunk in enumerate(chunks):
        session.execute(
            text(
                "INSERT INTO document_chunks(document_id, version_id, doc_type, scope, chunk_no, title, body) "
                "VALUES (:d, :v, :t, :s, :n, :title, :body)"
            ),
            {"d": document_id, "v": version_id, "t": doc_type, "s": scope, "n": i, "title": title, "body": chunk},
        )
    return len(chunks)


def remove_version(session: Session, version_id: int) -> None:
    session.execute(text("DELETE FROM document_chunks WHERE version_id = :vid"), {"vid": version_id})


@dataclass
class SearchHit:
    document_id: int
    version_id: int
    doc_type: str
    scope: str
    title: str
    snippet: str
    score: float


def _fts_query(query: str) -> str:
    terms = re.findall(r"[A-Za-z0-9']+", query)
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"' for t in terms)


def search(session: Session, query: str, *, limit: int = 8, doc_types: list[str] | None = None, scopes: list[str] | None = None) -> list[SearchHit]:
    sql = (
        "SELECT document_id, version_id, doc_type, scope, title, "
        "snippet(document_chunks, 6, '[', ']', ' … ', 24) AS snip, bm25(document_chunks) AS score "
        "FROM document_chunks WHERE document_chunks MATCH :q"
    )
    params: dict = {"q": _fts_query(query), "limit": limit}
    if doc_types:
        sql += " AND doc_type IN (" + ",".join(f":dt{i}" for i in range(len(doc_types))) + ")"
        params.update({f"dt{i}": d for i, d in enumerate(doc_types)})
    if scopes:
        sql += " AND scope IN (" + ",".join(f":sc{i}" for i in range(len(scopes))) + ")"
        params.update({f"sc{i}": s for i, s in enumerate(scopes)})
    sql += " ORDER BY score LIMIT :limit"
    rows = session.execute(text(sql), params).fetchall()
    return [SearchHit(r[0], r[1], r[2], r[3], r[4], r[5], float(r[6])) for r in rows]
