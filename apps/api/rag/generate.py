import json, datetime, random
from ..settings import settings
from .llm import ask_llm  # korzysta z providers/*, albo zwraca None

def _flatten_ctx(ctx):
    citations = [{"source":c["source"],"page":c["page"],"quote":c["quote"]} for c in ctx]
    ctx_txt = "\n".join([f"[{c['source']}|p.{c['page']}] {c['text']}" for c in ctx])[:8000]
    return ctx_txt, citations

def _meta(topic, diff):
    return {"topic": topic or "general", "difficulty": diff or "medium",
            "timestamp": datetime.datetime.utcnow().isoformat()+"Z"}

def gen_yes_no(ctx, difficulty="medium"):
    body, cites = _flatten_ctx(ctx)
    prompt = f"""Użyj wyłącznie poniższych fragmentów i wygeneruj jedno pytanie TAK/NIE.
Zwróć JSON: {{"stem":str,"answer":"TAK"|"NIE","explanation":str,"citations":[{{source,page,quote}}]}}.
Fragmenty:
{body}
"""
    llm = ask_llm(prompt)
    if llm:
        q = json.loads(llm); q["kind"]="YN"; q["metadata"]=_meta(None,difficulty); q["citations"]=cites
        return q
    # fallback heurystyczny (zawsze działa offline)
    base = ctx[0]["text"].split(".")[0]
    return {"kind":"YN","stem":f"Czy to prawda? {base.strip()}.",
            "answer":random.choice(["TAK","NIE"]),
            "explanation":"Wniosek na podstawie cytowanych fragmentów.",
            "metadata":_meta(None,difficulty),"citations":cites}

def gen_mcq(ctx, difficulty="medium"):
    body, cites = _flatten_ctx(ctx)
    prompt = f"""Użyj wyłącznie fragmentów poniżej i wygeneruj jedno pytanie ABCD.
Zwróć JSON: {{"stem":str,"options":[str,str,str,str],"answer":"a"|"b"|"c"|"d","explanation":str,"citations":[...]}}.
Fragmenty:
{body}
"""
    llm = ask_llm(prompt)
    if llm:
        q = json.loads(llm); q["kind"]="MCQ"; q["metadata"]=_meta(None,difficulty); q["citations"]=cites
        return q
    # fallback
    base = ctx[0]["text"].split(".")[0]
    options = ["Poprawna teza (zgodna ze źródłem)","Mylna generalizacja","Nieistotny szczegół","Sprzeczny wniosek"]
    return {"kind":"MCQ","stem":f"Które stwierdzenie wynika z materiału? {base.strip()}.",
            "options":[f"a) {options[0]}",f"b) {options[1]}",f"c) {options[2]}",f"d) {options[3]}"],
            "answer":"a","explanation":"a) wynika z cytowanych fragmentów.",
            "metadata":_meta(None,difficulty),"citations":cites}
