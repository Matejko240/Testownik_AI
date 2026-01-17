import sqlite3, json, os, hashlib

def _connect(db_path: str) -> sqlite3.Connection:
    """Otwiera połączenie SQLite z sensownymi ustawieniami współbieżności."""
    con = sqlite3.connect(db_path, timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=30000;")
    return con

def init_db(db_path: str):
    """Tworzy/aktualizuje schemat DB.

    Uwaga: plik schema.sql jest czytany z projektu (apps/api/sql/schema.sql).
    """
    con = _connect(db_path)
    try:
        cur = con.cursor()
        cur.executescript(open("apps/api/sql/schema.sql", "r", encoding="utf-8").read())

        # --- migracje lekkie (dla istniejących baz) ---
        cur.execute("PRAGMA table_info(sources)")
        cols = {row[1] for row in cur.fetchall()}
        if "sha256" not in cols:
            cur.execute("ALTER TABLE sources ADD COLUMN sha256 TEXT")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_sha256 ON sources(sha256)")

        con.commit()
    finally:
        con.close()

def get_source_id_by_sha256(sha256: str, db_path: str) -> int | None:
    """Zwraca id źródła, jeśli w bazie jest plik o tym sha256."""
    if not sha256:
        return None
    con = _connect(db_path)
    try:
        cur = con.cursor()
        cur.execute("SELECT id FROM sources WHERE sha256=? LIMIT 1", (sha256,))
        row = cur.fetchone()
        return int(row[0]) if row else None
    finally:
        con.close()

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def backfill_sources_sha256(src_dir: str, db_path: str) -> dict:
    """Uzupełnia sha256 dla już wgranych źródeł (jeśli wcześniej go nie było)."""
    con = _connect(db_path)
    updated = 0
    missing_file = 0
    try:
        cur = con.cursor()
        cur.execute("SELECT id, filename FROM sources WHERE sha256 IS NULL OR sha256='' ")
        rows = cur.fetchall()
        for sid, fname in rows:
            p = os.path.join(src_dir, fname)
            if not os.path.isfile(p):
                missing_file += 1
                continue
            sha = _sha256_file(p)
            cur.execute("UPDATE sources SET sha256=? WHERE id=?", (sha, sid))
            updated += 1
        con.commit()
        return {"updated": updated, "missing_file": missing_file}
    finally:
        con.close()

def save_question_with_citations(qid: str, q: dict, db_path: str):
    con = _connect(db_path)
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO questions(id,kind,stem,options,answer,explanation,metadata,created_at)
               VALUES(?,?,?,?,?,?,?,datetime('now'))""",
            (
                qid,
                q["kind"],
                q["stem"],
                json.dumps(q.get("options")),
                q["answer"],
                q["explanation"],
                json.dumps(q["metadata"]),
            ),
        )
        for c in q["citations"]:
            cur.execute(
                """INSERT OR IGNORE INTO question_citations(question_id,source_id,page,quote)
                   SELECT ?, id, ?, ? FROM sources WHERE filename=?""",
                (qid, c["page"], c["quote"], c["source"]),
            )
        con.commit()
    finally:
        con.close()

def insert_rating(qid: str, score: int, feedback: str | None, db_path: str):
    score = max(1, min(10, score))
    con = _connect(db_path)
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO ratings(question_id,score,feedback,created_at)
               VALUES(?,?,?,datetime('now'))""",
            (qid, score, feedback),
        )
        delta = 0.05 if score >= 8 else (-0.05 if score <= 3 else 0.0)
        if abs(delta) > 0:
            cur.execute(
                """UPDATE chunk_weights SET weight=weight+?
                   WHERE chunk_id IN (
                      SELECT c.id FROM chunks c
                      JOIN question_citations qc ON qc.source_id=c.source_id
                      JOIN questions q ON q.id=qc.question_id
                      WHERE q.id=?)""",
                (delta, qid),
            )
        con.commit()
    finally:
        con.close()
def list_sources(db_path: str, limit: int = 1000, offset: int = 0) -> dict:
    """Zwraca listę źródeł z DB (z paginacją) + total."""
    limit = max(1, min(int(limit), 5000))
    offset = max(0, int(offset))

    con = _connect(db_path)
    try:
        cur = con.cursor()

        cur.execute("SELECT COUNT(*) FROM sources")
        total = int(cur.fetchone()[0] or 0)

        cur.execute(
            """
            SELECT
              s.id,
              s.filename,
              s.mime,
              s.pages,
              s.imported_at,
              COALESCE(s.sha256, '') AS sha256,
              (SELECT COUNT(*) FROM chunks c WHERE c.source_id = s.id) AS chunks
            FROM sources s
            ORDER BY s.imported_at DESC, s.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        rows = cur.fetchall()

        items = []
        for r in rows:
            items.append(
                {
                    "id": int(r[0]),
                    "filename": r[1],
                    "mime": r[2],
                    "pages": int(r[3]) if r[3] is not None else None,
                    "imported_at": r[4],
                    "sha256": r[5],
                    "chunks": int(r[6]) if r[6] is not None else 0,
                }
            )

        return {"total": total, "items": items, "limit": limit, "offset": offset}
    finally:
        con.close()
def _json_loads_or_none(val):
    """Bezpiecznie parsuje pole JSON z SQLite (TEXT/JSON) do obiektu Pythona."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def get_question(question_id: str, db_path: str, with_quality: bool = True) -> dict | None:
    """Zwraca jedno pytanie zapisane w DB + cytowania + opcjonalnie jakość (avg_score/votes)."""
    if not question_id:
        return None

    con = _connect(db_path)
    try:
        cur = con.cursor()

        if with_quality:
            cur.execute(
                """
                SELECT
                  q.id, q.kind, q.stem, q.options, q.answer, q.explanation, q.metadata, q.created_at,
                  COALESCE(qq.avg_score, NULL) AS avg_score,
                  COALESCE(qq.votes, 0) AS votes
                FROM questions q
                LEFT JOIN question_quality qq ON qq.question_id = q.id
                WHERE q.id=?
                LIMIT 1
                """,
                (question_id,),
            )
        else:
            cur.execute(
                """
                SELECT q.id, q.kind, q.stem, q.options, q.answer, q.explanation, q.metadata, q.created_at,
                       NULL AS avg_score, 0 AS votes
                FROM questions q
                WHERE q.id=?
                LIMIT 1
                """,
                (question_id,),
            )

        row = cur.fetchone()
        if not row:
            return None

        qid, kind, stem, options, answer, explanation, metadata, created_at, avg_score, votes = row

        # cytowania
        cur.execute(
            """
            SELECT s.filename, qc.page, qc.quote
            FROM question_citations qc
            JOIN sources s ON s.id = qc.source_id
            WHERE qc.question_id=?
            ORDER BY s.filename ASC, qc.page ASC
            """,
            (qid,),
        )
        citations = [
            {"source": r[0], "page": int(r[1]), "quote": r[2]} for r in cur.fetchall() if r and r[0]
        ]

        qobj = {
            "kind": kind,
            "stem": stem,
            "options": _json_loads_or_none(options),
            "answer": answer,
            "explanation": explanation,
            "metadata": _json_loads_or_none(metadata) or {},
            "citations": citations,
        }

        out = {"question_id": qid, "question": qobj, "created_at": created_at}
        if with_quality:
            out["quality"] = {
                "avg_score": (float(avg_score) if avg_score is not None else None),
                "votes": int(votes or 0),
            }
        return out
    finally:
        con.close()


def list_questions(
    db_path: str,
    limit: int = 100,
    offset: int = 0,
    kind: str | None = None,
    topic: str | None = None,          # <-- NOWE
    with_citations: bool = True,
    with_quality: bool = True,
) -> dict:
    """Zwraca listę pytań zapisanych w DB (z paginacją)."""
    limit = max(1, min(int(limit), 5000))
    offset = max(0, int(offset))

    k = (kind or "").strip().upper()
    if k not in {"", "YN", "MCQ"}:
        k = ""  # ignoruj niepoprawny filtr

    t = (topic or "").strip().lower()

    con = _connect(db_path)
    try:
        cur = con.cursor()

        # --- budowa WHERE + params (bez limit/offset) ---
        where = []
        params = []

        if k:
            where.append("q.kind=?")
            params.append(k)

        # topic: preferuj JSON_EXTRACT, a jak SQLite nie ma JSON1 -> fallback LIKE
        use_json_extract = bool(t)
        if t:
            where.append("LOWER(json_extract(q.metadata,'$.topic')) = ?")
            params.append(t)

        where_sql = ("WHERE " + " AND ".join(where) + " ") if where else ""

        # --- total ---
        try:
            cur.execute(f"SELECT COUNT(*) FROM questions q {where_sql}", tuple(params))
            total = int(cur.fetchone()[0] or 0)
        except sqlite3.OperationalError:
            # fallback: JSON_EXTRACT niedostępny -> LIKE na JSON stringu
            if t and use_json_extract:
                where = [w for w in where if "json_extract" not in w]
                params = [p for p in params if p != t]
                where.append("LOWER(q.metadata) LIKE ?")
                params.append(f'%"topic": "{t}"%')
                where_sql = ("WHERE " + " AND ".join(where) + " ") if where else ""
            cur.execute(f"SELECT COUNT(*) FROM questions q {where_sql}", tuple(params))
            total = int(cur.fetchone()[0] or 0)

        # --- lista ---
        if with_quality:
            base_sql = (
                "SELECT q.id, q.kind, q.stem, q.options, q.answer, q.explanation, q.metadata, q.created_at, "
                "       COALESCE(qq.avg_score, NULL) AS avg_score, COALESCE(qq.votes, 0) AS votes "
                "FROM questions q "
                "LEFT JOIN question_quality qq ON qq.question_id = q.id "
            )
        else:
            base_sql = (
                "SELECT q.id, q.kind, q.stem, q.options, q.answer, q.explanation, q.metadata, q.created_at, "
                "       NULL AS avg_score, 0 AS votes "
                "FROM questions q "
            )

        sql = base_sql + where_sql + "ORDER BY q.created_at DESC, q.id DESC LIMIT ? OFFSET ?"
        params_list = list(params) + [limit, offset]

        try:
            cur.execute(sql, tuple(params_list))
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            # ten sam fallback dla listy (gdy JSON_EXTRACT nie ma)
            if t and use_json_extract:
                where = [w for w in where if "json_extract" not in w]
                params2 = [p for p in params if p != t]
                where.append("LOWER(q.metadata) LIKE ?")
                params2.append(f'%"topic": "{t}"%')
                where_sql = ("WHERE " + " AND ".join(where) + " ") if where else ""
                sql = base_sql + where_sql + "ORDER BY q.created_at DESC, q.id DESC LIMIT ? OFFSET ?"
                params_list = list(params2) + [limit, offset]
                cur.execute(sql, tuple(params_list))
                rows = cur.fetchall()
            else:
                raise

        qids = [r[0] for r in rows]
        citations_map: dict[str, list[dict]] = {qid: [] for qid in qids}

        if with_citations and qids:
            placeholders = ",".join(["?"] * len(qids))
            cur.execute(
                f"""
                SELECT qc.question_id, s.filename, qc.page, qc.quote
                FROM question_citations qc
                JOIN sources s ON s.id = qc.source_id
                WHERE qc.question_id IN ({placeholders})
                ORDER BY qc.question_id ASC, s.filename ASC, qc.page ASC
                """,
                tuple(qids),
            )
            for qid, fname, page, quote in cur.fetchall():
                citations_map.setdefault(qid, []).append(
                    {"source": fname, "page": int(page), "quote": quote}
                )

        items = []
        for (qid, qkind, stem, options, answer, explanation, metadata, created_at, avg_score, votes) in rows:
            qobj = {
                "kind": qkind,
                "stem": stem,
                "options": _json_loads_or_none(options),
                "answer": answer,
                "explanation": explanation,
                "metadata": _json_loads_or_none(metadata) or {},
                "citations": citations_map.get(qid, []) if with_citations else [],
            }
            out = {"question_id": qid, "question": qobj, "created_at": created_at}
            if with_quality:
                out["quality"] = {
                    "avg_score": (float(avg_score) if avg_score is not None else None),
                    "votes": int(votes or 0),
                }
            items.append(out)

        return {"total": total, "items": items, "limit": limit, "offset": offset}
    finally:
        con.close()
