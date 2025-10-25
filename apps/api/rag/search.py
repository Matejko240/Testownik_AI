import sqlite3, numpy as np
from ..settings import settings

def _load(con):
    cur = con.cursor()
    cur.execute("""SELECT c.id,c.source_id,c.page,c.text,c.quote,c.embedding,
                          COALESCE(w.weight,0.0) AS w
                   FROM chunks c LEFT JOIN chunk_weights w ON w.chunk_id=c.id""")
    rows = cur.fetchall()
    ids, src, page, text, quote, emb, w = [],[],[],[],[],[],[]
    for r in rows:
        ids.append(r[0]); src.append(r[1]); page.append(r[2]); text.append(r[3]); quote.append(r[4])
        emb.append(np.frombuffer(r[5], dtype=np.float32)); w.append(float(r[6]))
    return (np.vstack(emb) if emb else np.zeros((0,384),dtype=np.float32),
            ids, src, page, text, quote, np.asarray(w))

def rag_search(query:str, k:int, db_path:str):
    con = sqlite3.connect(db_path); cur = con.cursor()
    mat, ids, src, page, text, quote, w = _load(con)
    if mat.shape[0]==0: return []
    from .emb import embed_query
    qv = embed_query(query, model_name=settings.emb_model)
    sims = mat @ qv
    sims = sims * (1.0 + w)    # preferuj chunk’i z dobrymi ocenami
    idx = np.argsort(-sims)[:k]
    out=[]
    for i in idx:
        cur.execute("SELECT filename FROM sources WHERE id=?", (src[i],))
        fname = cur.fetchone()[0]
        out.append({"chunk_id":int(ids[i]),"source_id":int(src[i]),"source":fname,
                    "page":int(page[i]),"quote":quote[i],"text":text[i],"score":float(sims[i])})
    con.close()
    return out
