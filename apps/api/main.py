from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, uuid, json, hashlib
from typing import Literal
import random

from .settings import settings
from .rag.store import (
    init_db,
    save_question_with_citations,
    insert_rating,
    get_source_id_by_sha256,
    backfill_sources_sha256,
    list_sources,   
)

from .rag.ingest import ingest_files
from .rag.search import rag_search
from .rag.generate import gen_yes_no, gen_mcq

app = FastAPI(title="Testownik AI Backend", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
def _pick_ctx(ctx_all: list[dict], i: int, size: int) -> list[dict]:
    if not ctx_all:
        return []
    focus = ctx_all[i % len(ctx_all)]
    others = [c for c in ctx_all if c is not focus]
    random.shuffle(others)
    return [focus] + others[: max(0, size - 1)]

@app.on_event("startup")
def _startup():
    os.makedirs(settings.index_dir, exist_ok=True)
    os.makedirs(settings.src_dir, exist_ok=True)
    init_db(settings.db_path)
    # ważne: żeby deduplikacja działała też dla starych uploadów
    backfill_sources_sha256(settings.src_dir, db_path=settings.db_path)

class SearchReq(BaseModel):
    query: str
    k: int = 8

class GenReq(BaseModel):
    topic: str | None = None
    difficulty: str | None = "medium"
    n: int = 1
    provider: Literal["default", "none", "ollama", "openai"] = "default"

class RateReq(BaseModel):
    question_id: str
    score: int
    feedback: str | None = None

@app.post("/upload")
async def upload(files: list[UploadFile] = File(...)):
    dsts = []
    skipped = []
    seen_hashes = set()

    for f in files:
        data = await f.read()
        sha = hashlib.sha256(data).hexdigest()

        if sha in seen_hashes:
            skipped.append({"file": f.filename, "reason": "duplicate_in_request"})
            continue
        seen_hashes.add(sha)

        if get_source_id_by_sha256(sha, db_path=settings.db_path) is not None:
            skipped.append({"file": f.filename, "reason": "already_uploaded"})
            continue

        p = os.path.join(settings.src_dir, f.filename)
        with open(p, "wb") as w:
            w.write(data)
        dsts.append(p)

    stats = ingest_files(dsts, db_path=settings.db_path, index_dir=settings.index_dir, emb_model=settings.emb_model)
    return {"ingested": stats, "skipped": skipped}
@app.get("/providers")
def providers():
    # Co UI może wyświetlić w dropdownie
    available = ["default", "none", "openai", "ollama"]

    # Czy backend ma sensownie skonfigurowane klucze/URL
    configured = {
        "openai": bool(getattr(settings, "openai_api_key", None)),
        "ollama": bool(getattr(settings, "ollama_base_url", None)),
        "none": True,
        "default": True,
    }

    return {
        "default": settings.llm_provider,
        "available": available,
        "configured": configured,
    }

@app.get("/sources")
def get_sources(limit: int = 1000, offset: int = 0):
    """Lista wszystkich źródeł w bazie (z paginacją)."""
    return list_sources(settings.db_path, limit=limit, offset=offset)
@app.post("/sources/backfill")
def sources_backfill():
    """
    Uzupełnia sha256 dla rekordów w tabeli sources, jeśli puste.
    Zwraca liczbę zaktualizowanych rekordów.
    """
    updated = backfill_sources_sha256(settings.db_path, settings.src_dir)
    return {"updated": int(updated)}

@app.delete("/sources")
def clear_sources():
    """Czyści źródła: usuwa pliki i resetuje bazę."""
    removed_files = 0

    if os.path.isdir(settings.src_dir):
        for name in os.listdir(settings.src_dir):
            p = os.path.join(settings.src_dir, name)
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    removed_files += 1
                except Exception:
                    pass

    for p in [settings.db_path, settings.db_path + "-wal", settings.db_path + "-shm"]:
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    os.makedirs(settings.index_dir, exist_ok=True)
    os.makedirs(settings.src_dir, exist_ok=True)
    init_db(settings.db_path)
    return {"ok": True, "removed_files": removed_files}

@app.post("/search")
def search(req: SearchReq):
    return {"results": rag_search(req.query, k=req.k, db_path=settings.db_path)}

@app.post("/gen/yn")
def gen_yn(req: GenReq):
    ctx_all = rag_search(req.topic or "przegląd materiału", k=max(8, int(req.n) * 3), db_path=settings.db_path)
    items = []
    for i in range(max(1, int(req.n))):
        ctx = _pick_ctx(ctx_all, i, size=4)
        q = gen_yes_no(ctx, topic=req.topic, difficulty=req.difficulty, provider=req.provider, variant=i+1)
        qid = str(uuid.uuid4())
        save_question_with_citations(qid, q, db_path=settings.db_path)
        items.append({"question_id": qid, "question": q})
    return items[0] if int(req.n) == 1 else {"items": items}

@app.post("/gen/mcq")
def generate_mcq(req: GenReq):
    ctx_all = rag_search(req.topic or "przegląd materiału", k=max(10, int(req.n) * 3), db_path=settings.db_path)
    items = []
    for i in range(max(1, int(req.n))):
        ctx = _pick_ctx(ctx_all, i, size=6)
        q = gen_mcq(ctx, topic=req.topic, difficulty=req.difficulty, provider=req.provider, variant=i+1)
        qid = str(uuid.uuid4())
        save_question_with_citations(qid, q, db_path=settings.db_path)
        items.append({"question_id": qid, "question": q})
    return items[0] if int(req.n) == 1 else {"items": items}

@app.post("/rate")
def rate(req: RateReq):
    insert_rating(req.question_id, req.score, req.feedback, db_path=settings.db_path)
    return {"ok": True}
