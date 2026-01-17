import sqlite3, numpy as np, re
from ..settings import settings

_HEADER_PATTERNS = [
    re.compile(r"\b\d+\s*/\s*\d+\b"),
    re.compile(r"\b\d{4}\s*/\s*\d{4}\b"),
]

def _looks_like_header(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return True
    if len(s) < 18:
        return True
    for rx in _HEADER_PATTERNS:
        if rx.search(s):
            return True
    return False

def _pick_snippet(quote: str, text: str, max_len: int = 180) -> str:
    q = (quote or "").strip()
    if q and not _looks_like_header(q):
        s = q
    else:
        lines = [ln.strip() for ln in re.split(r"[\r\n]+", text or "") if ln.strip()]
        s = ""
        for ln in lines:
            if not _looks_like_header(ln):
                s = ln
                break
        if not s:
            s = " ".join((text or "").split())
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s

# --- CACHE ---
_CACHE = None  # tuple: (mat, ids, src, page, text, quote, w, src_map, n_chunks)

def invalidate_cache():
    global _CACHE
    _CACHE = None

def _load_all(con):
    cur = con.cursor()
    cur.execute("""SELECT c.id,c.source_id,c.page,c.text,c.quote,c.embedding,
                          COALESCE(w.weight,0.0) AS w
                   FROM chunks c LEFT JOIN chunk_weights w ON w.chunk_id=c.id""")
    rows = cur.fetchall()

    ids, src, page, text, quote, emb, w = [], [], [], [], [], [], []
    for r in rows:
        ids.append(r[0]); src.append(r[1]); page.append(r[2]); text.append(r[3]); quote.append(r[4])
        emb.append(np.frombuffer(r[5], dtype=np.float32)); w.append(float(r[6]))

    mat = (np.vstack(emb) if emb else np.zeros((0,384), dtype=np.float32))
    w = np.asarray(w, dtype=np.float32)

    cur.execute("SELECT id, filename FROM sources")
    src_map = {int(i): fn for (i, fn) in cur.fetchall()}

    n_chunks = mat.shape[0]
    return mat, ids, src, page, text, quote, w, src_map, n_chunks

def rag_search(query: str, k: int, db_path: str):
    global _CACHE
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()

        # cache warm / invalidate if chunk count changed
        cur.execute("SELECT COUNT(*) FROM chunks")
        n_now = int(cur.fetchone()[0] or 0)

        if _CACHE is None or _CACHE[-1] != n_now:
            _CACHE = _load_all(con)

        mat, ids, src, page, text, quote, w, src_map, _n = _CACHE

        if mat.shape[0] == 0:
            return []

        from .emb import embed_query
        qv = embed_query(query, model_name=settings.emb_model)
        sims = mat @ qv
        sims = sims * (1.0 + w)

        idx = np.argsort(-sims)[: max(k * 3, k)]
        out = []
        for i in idx:
            fname = src_map.get(int(src[i]), "unknown")
            snippet = _pick_snippet(quote[i], text[i])
            out.append({
                "chunk_id": int(ids[i]),
                "source_id": int(src[i]),
                "source": fname,
                "page": int(page[i]),
                "quote": snippet,
                "text": text[i],
                "score": float(sims[i]),
            })
            if len(out) >= k:
                break
        return out
    finally:
        con.close()
