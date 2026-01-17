from fastapi import FastAPI, UploadFile, File, HTTPException
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
    backfill_questions_fingerprint,
    get_question_id_by_fingerprint,
    make_question_fingerprint,
    list_recent_question_stems,
    list_sources,
    get_question,
    list_questions,
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
    backfill_questions_fingerprint(db_path=settings.db_path)


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
    n = max(1, int(req.n))
    # więcej kontekstu => większa szansa na unikalne pytania
    ctx_all = rag_search(req.topic or "przegląd materiału", k=max(30, n * 12), db_path=settings.db_path)
    random.shuffle(ctx_all)

    recent_ban = list_recent_question_stems(settings.db_path, kind="YN", topic=req.topic, limit=40)

    items = []
    used_fps: set[str] = set()
    used_stems: list[str] = []

    for i in range(n):
        added = False

        for attempt in range(12):
            ctx = _pick_ctx(ctx_all, i + attempt, size=4)

            ban = (recent_ban + used_stems)[-40:]
            q = gen_mcq(ctx, topic=req.topic, difficulty=req.difficulty, provider=req.provider, variant=i + 1 + attempt)

            # Jeśli generator wpadł w fallback (debug.fallback_reason), to nie zapisujmy takiego pytania.
            # Lepiej zwrócić mniej pytań niż utrwalać bełt typu „zgodne z cytowanym fragmentem”.
            if req.provider != "none" and isinstance(q, dict) and isinstance(q.get("debug"), dict):
                if q["debug"].get("fallback_reason"):
                    continue

            fp = make_question_fingerprint(q.get("kind", "YN"), q.get("stem", ""), q.get("options"))

            # duplikat w tym samym batchu
            if fp in used_fps:
                continue

            # duplikat w bazie (wcześniej wygenerowane)
            if get_question_id_by_fingerprint(fp, db_path=settings.db_path) is not None:
                continue

            qid = str(uuid.uuid4())
            try:
                save_question_with_citations(qid, q, db_path=settings.db_path, fingerprint=fp)
            except Exception:
                # np. wyścig / unique constraint — spróbuj jeszcze raz
                continue

            used_fps.add(fp)
            used_stems.append(q.get("stem", ""))
            items.append({"question_id": qid, "question": q})
            added = True
            break

        if not added:
            # brak możliwości wyprodukowania kolejnego unikalnego pytania w limicie prób
            break

    return items[0] if n == 1 else {"items": items}

@app.post("/gen/mcq")
def generate_mcq(req: GenReq):
    n = max(1, int(req.n))
    ctx_all = rag_search(req.topic or "przegląd materiału", k=max(40, n * 12), db_path=settings.db_path)
    random.shuffle(ctx_all)

    recent_ban = list_recent_question_stems(settings.db_path, kind="MCQ", topic=req.topic, limit=40)

    items = []
    used_fps: set[str] = set()
    used_stems: list[str] = []

    for i in range(n):
        added = False

        for attempt in range(12):
            ctx = _pick_ctx(ctx_all, i + attempt, size=6)

            ban = (recent_ban + used_stems)[-40:]
            q = gen_mcq(ctx, topic=req.topic, difficulty=req.difficulty, provider=req.provider, variant=i + 1 + attempt)

            fp = make_question_fingerprint(q.get("kind", "MCQ"), q.get("stem", ""), q.get("options"))

            if fp in used_fps:
                continue
            if get_question_id_by_fingerprint(fp, db_path=settings.db_path) is not None:
                continue

            qid = str(uuid.uuid4())
            try:
                save_question_with_citations(qid, q, db_path=settings.db_path, fingerprint=fp)
            except Exception:
                continue

            used_fps.add(fp)
            used_stems.append(q.get("stem", ""))
            items.append({"question_id": qid, "question": q})
            added = True
            break

        if not added:
            break

    return items[0] if n == 1 else {"items": items}


@app.post("/rate")
def rate(req: RateReq):
    insert_rating(req.question_id, req.score, req.feedback, db_path=settings.db_path)
    return {"ok": True}
@app.get("/questions")
def questions(
    limit: int = 100,
    offset: int = 0,
    kind: str | None = None,
    topic: str | None = None,          # <-- NOWE
    with_citations: bool = True,
    with_quality: bool = True,
):
    """Lista zapisanych pytań (z paginacją).

    Parametry:
      - kind: "YN" | "MCQ" | None
      - topic: filtr po metadata.topic (np. "algorytmy")
      - with_citations: czy dołączać cytowania
      - with_quality: czy dołączać avg_score/votes (view question_quality)
    """
    return list_questions(
        settings.db_path,
        limit=limit,
        offset=offset,
        kind=kind,
        topic=topic,                     # <-- NOWE
        with_citations=with_citations,
        with_quality=with_quality,
    )


@app.get("/questions/{question_id}")
def question(question_id: str, with_quality: bool = True):
    """Jedno pytanie po ID (np. do ponownego wyświetlenia/rate)."""
    q = get_question(question_id, db_path=settings.db_path, with_quality=with_quality)
    if not q:
        raise HTTPException(status_code=404, detail="question_not_found")
    return q
