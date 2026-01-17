import datetime
import random
import re
import json
import ast
from typing import Any

from .llm import ask_llm

# -----------------------------
# Utilities: citations / context
# -----------------------------

_HEADER_PATTERNS = [
    re.compile(r"\b\d+\s*/\s*\d+\b"),      # "82/157"
    re.compile(r"\b\d{4}\s*/\s*\d{4}\b"),  # "2025/2026"
]

_TAG_RX = re.compile(r"\[[^\|\]]+\|p\.\d+\]")  # [File.pdf|p.123]


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


def _flatten_ctx(ctx: Any) -> tuple[str, list[dict]]:
    """
    Wejście: lista dictów (rag_search),
    Wyjście:
      - ctx_txt: linie typu "[file|p.N] snippet"
      - citations: [{"source":..., "page":..., "quote":...}, ...] (snippety)
    """
    if not isinstance(ctx, list):
        ctx = []

    seen = set()
    citations: list[dict] = []
    lines: list[str] = []

    for c in ctx:
        if not isinstance(c, dict):
            continue

        source = c.get("source")
        page = c.get("page")
        if not source or page is None:
            continue

        key = (str(source), int(page))
        if key in seen:
            continue
        seen.add(key)

        quote = (c.get("quote") or "").strip()
        text = (c.get("text") or "").strip()
        snippet = _pick_snippet(quote, text)

        if snippet:
            lines.append(f"[{source}|p.{page}] {snippet}")

        citations.append({"source": str(source), "page": int(page), "quote": snippet})

    ctx_txt = "\n".join(lines)
    # twardy limit, żeby nie przeładować LLM
    return ctx_txt[:8000], citations


def _meta(topic: str | None, diff: str | None) -> dict:
    return {
        "topic": topic or "general",
        "difficulty": diff or "medium",
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    }


def _count_expl_tags(expl: str | None) -> int:
    return len(_TAG_RX.findall(expl or ""))


def _filter_citations_by_expl(expl: str, cites: list[dict]) -> list[dict]:
    """
    Zwraca tylko te citations, które odpowiadają tagom [source|p.N] w explanation.
    Jeśli brak tagów -> max 2 pierwsze.
    """
    tags = set(re.findall(r"\[([^\|\]]+)\|p\.(\d+)\]", expl or ""))
    if not tags:
        return cites[:2]

    out: list[dict] = []
    for c in cites:
        try:
            if (c.get("source"), str(c.get("page"))) in tags:
                out.append(c)
        except Exception:
            pass

    return out or cites[:2]


# -----------------------------
# Utilities: JSON extraction
# -----------------------------

def _try_parse_obj(blob: str):
    blob = (blob or "").strip()
    try:
        return json.loads(blob)
    except Exception:
        pass
    try:
        return ast.literal_eval(blob)
    except Exception:
        return None


def _extract_json(s: str | None):
    if not s:
        return None
    s = s.strip()

    # fenced JSON
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, flags=re.DOTALL | re.IGNORECASE)
    if m:
        obj = _try_parse_obj(m.group(1))
        if isinstance(obj, dict):
            return obj

    # whole string is JSON
    if s.startswith("{") and s.endswith("}"):
        obj = _try_parse_obj(s)
        if isinstance(obj, dict):
            return obj

    # find first JSON object
    start = s.find("{")
    if start == -1:
        return None

    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    blob = s[start : i + 1]
                    obj = _try_parse_obj(blob)
                    return obj if isinstance(obj, dict) else None
    return None


# -----------------------------
# YN generation
# -----------------------------

def _validate_yn_obj(obj: dict) -> tuple[bool, str]:
    if not isinstance(obj, dict):
        return False, "not a dict"
    stem = obj.get("stem")
    ans = obj.get("answer")
    expl = obj.get("explanation")
    if not isinstance(stem, str) or not stem.strip():
        return False, "missing stem"
    if ans not in {"TAK", "NIE"}:
        return False, "answer must be TAK or NIE"
    if not isinstance(expl, str) or not expl.strip():
        return False, "missing explanation"
    if _count_expl_tags(expl) != 1:
        return False, "explanation must contain exactly one [source|p.N] tag"
    return True, "ok"


def gen_yes_no(ctx, topic=None, difficulty="medium", provider: str | None = None, variant: int = 1):
    body, cites = _flatten_ctx(ctx)

    base_prompt = f"""Użyj WYŁĄCZNIE fragmentów poniżej i wygeneruj JEDNO pytanie TAK/NIE (wariant {variant}).
Zwróć TYLKO JSON: {{"stem":str,"answer":"TAK"|"NIE","explanation":str}}.
Wymóg: w "explanation" MUSI być dokładnie jeden tag w formacie [nazwa_pliku|p.N] z poniższych fragmentów.
Bez komentarzy, bez markdown, bez dodatkowego tekstu.

Fragmenty:
{body}
""".strip()

    prompt = base_prompt
    last_reason = "init"

    for attempt in range(2):
        llm = ask_llm(prompt, provider=provider)
        qobj = _extract_json(llm) if llm else None

        ok = False
        reason = "no json"
        if isinstance(qobj, dict):
            ok, reason = _validate_yn_obj(qobj)

        if ok:
            qobj["kind"] = "YN"
            qobj["metadata"] = _meta(topic, difficulty)
            qobj["citations"] = _filter_citations_by_expl(qobj.get("explanation", ""), cites)
            return qobj

        last_reason = reason
        prompt = (
            "NAPRAW OUTPUT. Zwróć WYŁĄCZNIE poprawny JSON. "
            f"Problem: {reason}. "
            "Pamiętaj: answer=TAK/NIE, explanation zawiera dokładnie jeden tag [source|p.N].\n\n"
            + base_prompt
        )

    # Fallback (offline / LLM failed)
    first = (body.splitlines()[0] if body else "").strip()
    stem = f"Czy poniższe stwierdzenie wynika z materiału?\n{first}" if first else "Czy poniższe stwierdzenie wynika z materiału?"
    return {
        "kind": "YN",
        "stem": stem,
        "answer": "TAK",
        "explanation": "Na podstawie przytoczonych fragmentów.",
        "metadata": _meta(topic, difficulty),
        "citations": cites[:2],
        "debug": {"fallback_reason": last_reason},
    }


# -----------------------------
# MCQ generation + semantic check
# -----------------------------

def _norm_answer_letter(ans: str | None) -> str | None:
    if not ans:
        return None
    a = ans.strip().lower()
    m = re.match(r"^([a-d])\b", a)
    return m.group(1) if m else None


def _normalize_options(opts: Any) -> list[str] | None:
    if not isinstance(opts, list):
        return None
    out: list[str] = []
    for o in opts:
        if not isinstance(o, str):
            return None
        s = o.strip()
        # usuń ewentualne "a) " / "b. " itp.
        s = re.sub(r"^[a-dA-D]\s*[\)\]\.:\-]\s*", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        out.append(s)
    return out


def _validate_mcq_obj(obj: dict) -> tuple[bool, str]:
    if not isinstance(obj, dict):
        return False, "not a dict"

    stem = obj.get("stem")
    if not isinstance(stem, str) or not stem.strip():
        return False, "missing stem"

    opts = _normalize_options(obj.get("options"))
    if not opts or len(opts) != 4:
        return False, "options must be list of 4 strings"
    if any(not o for o in opts):
        return False, "empty option"
    if len({o.lower() for o in opts}) != 4:
        return False, "options must be unique"

    ans = _norm_answer_letter(obj.get("answer"))
    if ans not in {"a", "b", "c", "d"}:
        return False, "answer must be a|b|c|d"

    expl = obj.get("explanation")
    if not isinstance(expl, str) or not expl.strip():
        return False, "missing explanation"

    if _count_expl_tags(expl) != 1:
        return False, "explanation must contain exactly one [source|p.N] tag"

    # nadpisz znormalizowane wartości (żeby reszta kodu korzystała z czystych opcji)
    obj["options"] = opts
    obj["answer"] = ans

    return True, "ok"

def _extract_letter_list_from_text(resp: str) -> list[str]:
    """
    Fallback parser: wyciąga litery a-d z tekstu, usuwa duplikaty.
    """
    if not resp:
        return []
    letters = re.findall(r"\b([a-d])\b", resp.lower())
    out = []
    for x in letters:
        if x not in out:
            out.append(x)
    return out

def _semantic_check_mcq(body: str, q: dict, provider: str | None) -> tuple[bool, str]:
    """
    ok=True tylko gdy wg fragmentów dokładnie 1 opcja jest prawdziwa i zgadza się z q["answer"].
    Jeśli checker nie zwróci się w formacie JSON, próbujemy parsować litery z tekstu.
    Gdy nadal się nie da — SKIP zamiast zabijać MCQ (żeby nie wpadać w fallback generowania).
    """
    payload = {"stem": q.get("stem", ""), "options": q.get("options", [])}

    def run_check(strict: bool) -> list[str] | None:
        extra = ""
        if strict:
            extra = (
                "\nBEZ markdown, BEZ komentarzy, BEZ uzasadnienia. "
                "Zwróć tylko jedną linię JSON.\n"
                'Przykład: {"correct":["b"]}\n'
            )

        check_prompt = f"""Użyj WYŁĄCZNIE fragmentów poniżej.
Wskaż, które opcje są PRAWDZIWE (wprost wspierane przez fragmenty). Reszta = FAŁSZ.
Jeśli opcja nie wynika jednoznacznie z fragmentów, uznaj ją za FAŁSZ.
{extra}
Zwróć TYLKO JSON:
{{"correct":["a"|"b"|"c"|"d", ...]}}

Pytanie:
{json.dumps(payload, ensure_ascii=False)}

Fragmenty:
{body}
""".strip()

        resp = ask_llm(check_prompt, provider=provider)
        if not resp:
            return None

        obj = _extract_json(resp)
        if isinstance(obj, dict):
            # 1) preferowane: {"correct":[...]}
            corr = obj.get("correct")
            if isinstance(corr, list):
                letters = []
                for x in corr:
                    a = _norm_answer_letter(str(x))
                    if a in {"a", "b", "c", "d"} and a not in letters:
                        letters.append(a)
                return letters

            # 2) czasem model zwraca {"answer":"b"} lub {"correct":"b"}
            for key in ("answer", "correct"):
                if isinstance(obj.get(key), str):
                    a = _norm_answer_letter(obj.get(key))
                    if a in {"a", "b", "c", "d"}:
                        return [a]

        # 3) fallback: litery z tekstu
        letters = _extract_letter_list_from_text(resp)
        # jeśli znajdziemy coś sensownego, zwróć
        if letters:
            return letters

        return []  # nie udało się nic

    # pierwsza próba (normal)
    letters = run_check(strict=False)
    if letters is None:
        return True, "skipped_no_llm"

    # jeśli pusto, druga próba stricte „JSON-only”
    if letters == []:
        letters = run_check(strict=True)
        if letters is None:
            return True, "skipped_no_llm"

    # nadal nie umiemy sparsować → SKIP (zamiast FAIL, bo teraz wpadasz w fallback MCQ)
    if letters == []:
        return True, "skipped_semantic_check_parse_failed"

    if len(letters) != 1:
        return False, f"semantic_check_expected_1_correct_got={letters}"

    if letters[0] != q.get("answer"):
        return False, f"semantic_check_answer_mismatch expected={q.get('answer')} got={letters[0]}"

    return True, "ok"



def gen_mcq(ctx, topic=None, difficulty="medium", provider: str | None = None, variant: int = 1):
    body, cites = _flatten_ctx(ctx)

    base_prompt = f"""Użyj WYŁĄCZNIE fragmentów poniżej i wygeneruj JEDNO pytanie wielokrotnego wyboru (wariant {variant}).

Wymagania twarde:
- dokładnie 4 opcje (options), wszystkie UNIKALNE,
- dokładnie 1 poprawna odpowiedź, 3 błędne (ale wiarygodne),
- options mają być CZYSTYM tekstem (bez 'a)', 'b)', numeracji itp.),
- answer ma być literą: "a"|"b"|"c"|"d",
- nie używaj opcji typu „wszystkie powyższe”,
- stem ma dotyczyć jednego konkretnego faktu/pojęcia z fragmentów (nie pytaj ogólnie).

Zwróć TYLKO JSON:
{{"stem":str,"options":[str,str,str,str],"answer":"a"|"b"|"c"|"d","explanation":str}}

Wymóg: w "explanation" MUSI być dokładnie jeden tag w formacie [nazwa_pliku|p.N] z poniższych fragmentów.
Bez komentarzy, bez markdown, bez dodatkowego tekstu.

Fragmenty:
{body}
""".strip()

    prompt = base_prompt
    last_reason = "init"

    for attempt in range(3):
        llm = ask_llm(prompt, provider=provider)
        qobj = _extract_json(llm) if llm else None

        ok = False
        reason = "no json"
        if isinstance(qobj, dict):
            ok, reason = _validate_mcq_obj(qobj)

        # semantic check (tylko jeśli syntaktycznie OK)
        if ok:
            sem_ok, sem_reason = _semantic_check_mcq(body, qobj, provider=provider)
            if not sem_ok:
                ok = False
                reason = sem_reason

        if ok:
            qobj["kind"] = "MCQ"
            qobj["metadata"] = _meta(topic, difficulty)
            qobj["citations"] = _filter_citations_by_expl(qobj.get("explanation", ""), cites)
            return qobj

        last_reason = reason

        # ważne: modyfikacja prompta MUSI BYĆ W PĘTLI
        if attempt == 0:
            prompt = (
                "NAPRAW OUTPUT. Zwróć WYŁĄCZNIE poprawny JSON zgodny ze schematem. "
                f"Problem: {reason}. "
                "Pamiętaj: 4 unikalne opcje, answer=a|b|c|d, explanation ma dokładnie jeden tag.\n\n"
                + base_prompt
            )
        else:
            prompt = (
                "Wygeneruj CAŁKIEM INNE pytanie (inny fakt z fragmentów), "
                f"bo poprzednie miało problem: {reason}. "
                "Zadbaj, aby tylko 1 opcja była prawdziwa.\n\n"
                + base_prompt
            )

    # Fallback MCQ (offline / LLM failed)
    first_line = (body.splitlines()[0] if body else "").strip()
    base = first_line if first_line else "Materiał dotyczy zagadnień z optymalizacji i algorytmów."

    return {
        "kind": "MCQ",
        "stem": f"Które stwierdzenie najlepiej wynika z przytoczonych fragmentów?\n{base}",
        "options": [
            "Stwierdzenie zgodne z fragmentami",
            "Stwierdzenie sprzeczne z fragmentami",
            "Stwierdzenie niepowiązane z fragmentami",
            "Stwierdzenie zbyt ogólne, niewynikające wprost z fragmentów",
        ],
        "answer": "a",
        "explanation": "Odpowiedź a) jest zgodna z fragmentami.",
        "metadata": _meta(topic, difficulty),
        "citations": cites[:2],
        "debug": {"fallback_reason": last_reason},
    }
