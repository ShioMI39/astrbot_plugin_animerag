from __future__ import annotations

from typing import Any

PER_ENTITY = 2
MAX_CHUNKS = 4
MAX_ENTITIES = 2


def expand_bodies(
    idx: Any,
    rows: list[dict],
    budget: int,
    *,
    per_entity: int = PER_ENTITY,
    max_entities: int = MAX_ENTITIES,
) -> int:
    """原地把正文块插到命中的角色行后面，返回补了几条。"""
    if idx is None or budget <= 0 or not rows:
        return 0

    have: set[str] = {str(r.get("id")) for r in rows if r.get("id")}
    done_entities: set[int] = set()
    added = 0
    i = 0
    while i < len(rows) and added < budget and len(done_entities) < max_entities:
        row = rows[i]
        if row.get("kind") != "character":
            i += 1
            continue
        ref_id = int(row.get("ref_id") or 0)
        if not ref_id or ref_id in done_entities:
            i += 1
            continue
        done_entities.add(ref_id)

        insert_at = i + 1
        for ch in idx.chunks_by_ref("moegirl", ref_id, limit=per_entity):
            if added >= budget:
                break
            cid = str(ch.get("id") or "")
            if cid and cid in have:
                continue
            if cid:
                have.add(cid)
            rows.insert(insert_at, {
                "id": ch.get("id"),
                "kind": ch.get("kind") or "moegirl",
                "ref_id": ch.get("ref_id", ref_id),
                "title": ch.get("title") or "",
                "text": ch.get("text") or "",
                "section": ch.get("section"),
                "score": None,
                "rerank_score": None,
                "source": "expand",
            })
            insert_at += 1
            added += 1
        i = insert_at
    return added
