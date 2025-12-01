from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, uuid, json

from .settings import settings
from .rag.store import init_db, save_question_with_citations, insert_rating
from .rag.ingest import ingest_files
from .rag.search import rag_search
from .rag.generate import gen_yes_no, gen_mcq


app = FastAPI(title="Testownik AI Backend", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup():
    os.makedirs(settings.index_dir, exist_ok=True)
    os.makedirs(settings.src_dir, exist_ok=True)
    init_db(settings.db_path)

class SearchReq(BaseModel):
    query: str
    k: int = 8

class GenReq(BaseModel):
    topic: str | None = None
    difficulty: str | None = "medium"

class RateReq(BaseModel):
    question_id: str
    score: int
    feedback: str | None = None

@app.post("/upload")
async def upload(files: list[UploadFile] = File(...)):
    dsts = []
    for f in files:
        p = os.path.join(settings.src_dir, f.filename)
        with open(p,"wb") as w:
            w.write(await f.read())
        dsts.append(p)
    stats = ingest_files(dsts, db_path=settings.db_path, index_dir=settings.index_dir, emb_model=settings.emb_model)
    return {"ingested": stats}

@app.post("/search")
def search(req: SearchReq):
    return {"results": rag_search(req.query, k=req.k, db_path=settings.db_path)}

@app.post("/gen/yn")
def gen_yn(req: GenReq):
    ctx = rag_search(req.topic or "przegląd materiału", k=8, db_path=settings.db_path)
    q = gen_yes_no(ctx, topic=req.topic, difficulty=req.difficulty)
    qid = str(uuid.uuid4())
    save_question_with_citations(qid, q, db_path=settings.db_path)
    return {"question_id": qid, "question": q}

@app.post("/gen/mcq")
def generate_mcq(req: GenReq):  # ← inna nazwa funkcji endpointu
    ctx = rag_search(req.topic or "przegląd materiału", k=10, db_path=settings.db_path)
    q = gen_mcq(ctx,topic=req.topic, difficulty=req.difficulty)  # ← to jest funkcja z rag.generate
    qid = str(uuid.uuid4())
    save_question_with_citations(qid, q, db_path=settings.db_path)
    return {"question_id": qid, "question": q}

@app.post("/rate")
def rate(req: RateReq):
    insert_rating(req.question_id, req.score, req.feedback, db_path=settings.db_path)
    return {"ok": True}
