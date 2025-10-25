import sqlite3, json

def init_db(db_path:str):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript(open("apps/api/sql/schema.sql","r",encoding="utf-8").read())
    con.commit(); con.close()

def save_question_with_citations(qid:str, q:dict, db_path:str):
    con=sqlite3.connect(db_path); cur=con.cursor()
    cur.execute("""INSERT INTO questions(id,kind,stem,options,answer,explanation,metadata,created_at)
                   VALUES(?,?,?,?,?,?,?,datetime('now'))""",
                (qid,q["kind"],q["stem"],json.dumps(q.get("options")),q["answer"],q["explanation"],json.dumps(q["metadata"])))
    for c in q["citations"]:
        cur.execute("""INSERT INTO question_citations(question_id,source_id,page,quote)
                       SELECT ?, id, ?, ? FROM sources WHERE filename=?""",
                    (qid, c["page"], c["quote"], c["source"]))
    con.commit(); con.close()

def insert_rating(qid:str, score:int, feedback:str|None, db_path:str):
    score = max(1, min(10, score))
    con=sqlite3.connect(db_path); cur=con.cursor()
    cur.execute("""INSERT INTO ratings(question_id,score,feedback,created_at)
                   VALUES(?,?,?,datetime('now'))""", (qid,score,feedback))
    # aktualizacja wag chunków (prosta heurystyka: +0.05 za ocenę>=8, -0.05 za <=3)
    delta = 0.05 if score>=8 else (-0.05 if score<=3 else 0.0)
    if abs(delta)>0:
        cur.execute("""UPDATE chunk_weights SET weight=weight+?
                       WHERE chunk_id IN (
                          SELECT c.id FROM chunks c
                          JOIN question_citations qc ON qc.source_id=c.source_id
                          JOIN questions q ON q.id=qc.question_id
                          WHERE q.id=?)""", (delta, qid))
    con.commit(); con.close()
