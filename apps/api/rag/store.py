import sqlite3, json

def _connect(db_path: str) -> sqlite3.Connection:
    """Otwiera połączenie SQLite z sensownymi ustawieniami współbieżności."""
    con = sqlite3.connect(db_path, timeout=30.0)
    # trochę lepsza współbieżność (WAL)
    con.execute("PRAGMA journal_mode=WAL;")
    # ile ms SQLite ma czekać przy zablokowanej bazie
    con.execute("PRAGMA busy_timeout=30000;")
    return con

def init_db(db_path: str):
    con = _connect(db_path)
    try:
        cur = con.cursor()
        cur.executescript(open("apps/api/sql/schema.sql", "r", encoding="utf-8").read())
        con.commit()
    finally:
        con.close()

def save_question_with_citations(qid: str, q: dict, db_path: str):
    """Zapisuje pytanie + cytaty.

    - zawsze zamyka połączenie (również przy błędzie),
    - ignoruje duplikaty cytatów dla tego samego pytania.
    """
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
        # prosta heurystyka do wag chunków
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
