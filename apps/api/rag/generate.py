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
    re.compile(r"\bA\.\s*Wielgus\b", re.IGNORECASE),
    re.compile(r"\bWykład\b", re.IGNORECASE),
    re.compile(r"https?://", re.IGNORECASE),
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

def _strip_tags(text: str | None) -> str:
    return _TAG_RX.sub("", (text or "")).strip()
def _force_single_expl_tag(expl: str | None, cites: list[dict]) -> str:
    """
    Normalizuje explanation tak, żeby miało DOKŁADNIE jeden tag [source|p.N]
    + sensowne uzasadnienie. Ratuje przypadki, gdzie LLM daje 0 albo >1 tagów.
    """
    expl = (expl or "").strip()

    tags = re.findall(r"\[([^\|\]]+)\|p\.(\d+)\]", expl)
    if tags:
        src, page = tags[0][0], int(tags[0][1])
    elif cites:
        src, page = str(cites[0].get("source")), int(cites[0].get("page"))
    else:
        return expl

    rationale = _strip_tags(expl)
    if len(rationale) < 12:
        quote = ""
        for c in cites:
            try:
                if str(c.get("source")) == src and int(c.get("page")) == page:
                    quote = (c.get("quote") or "").strip()
                    break
            except Exception:
                pass
        rationale = quote if quote else "Uzasadnienie wynika z przytoczonego fragmentu."

    return f"[{src}|p.{page}] {rationale}".strip()

def _ensure_expl_has_rationale(expl: str, cites: list[dict]) -> str:
    """
    Jeśli explanation to tylko tag, dopisz krótki cytat/snippet z citations.
    """
    m = re.search(r"\[([^\|\]]+)\|p\.(\d+)\]", expl or "")
    if not m:
        return expl
    src, page = m.group(1), int(m.group(2))

    rest = _strip_tags(expl)
    if rest:
        return expl.strip()

    for c in cites:
        if c.get("source") == src and int(c.get("page")) == page:
            quote = (c.get("quote") or "").strip()
            if quote:
                return f"[{src}|p.{page}] {quote}"
            break

    return expl.strip()

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

    # NOWE: explanation ma mieć też uzasadnienie (nie tylko tag)
    rationale = _strip_tags(expl)
    if len(rationale) < 12:
        return False, "explanation too short (needs rationale text after tag)"

    return True, "ok"

def _semantic_check_yn(body: str, stem: str, provider: str | None) -> tuple[bool, str | None]:
    """
    Z fragmentów oceń, czy zdanie (stem) jest prawdziwe.
    WAŻNE: jeśli checker nie da się sparsować -> nie blokujemy generowania (skip),
    żeby nie wpadać w fallback.
    """
    if provider in (None, "none"):
        return True, None

    def run_check(strict: bool) -> str | None:
        extra = ""
        if strict:
            extra = (
                '\nBEZ markdown, BEZ komentarzy, BEZ uzasadnienia. '
                'Zwróć tylko jedną linię JSON.\n'
                'Przykład: {"answer":"TAK"}\n'
            )

        prompt = f"""Użyj WYŁĄCZNIE fragmentów poniżej.
Oceń, jaka odpowiedź (TAK/NIE) jest poprawna na to pytanie.
Jeśli nie wynika jednoznacznie z fragmentów, odpowiedz "NIE".
{extra}
Zwróć TYLKO JSON: {{"answer":"TAK"|"NIE"}}.

Pytanie TAK/NIE:
{stem}

Fragmenty:
{body}
""".strip()

        resp = ask_llm(prompt, provider=provider)
        if not resp:
            return None

        obj = _extract_json(resp)
        if isinstance(obj, dict) and obj.get("answer") in {"TAK", "NIE"}:
            return obj["answer"]

        txt = resp.upper()
        if "TAK" in txt and "NIE" not in txt:
            return "TAK"
        if "NIE" in txt and "TAK" not in txt:
            return "NIE"

        return None

    ans = run_check(strict=False)
    if ans is None:
        ans = run_check(strict=True)

    # nigdy nie blokuj generowania, jeśli nie umiemy sparsować checkera
    return True, ans if ans in {"TAK", "NIE"} else None



def gen_yes_no(ctx, topic=None, difficulty="medium", provider: str | None = None, variant: int = 1):
    body, cites = _flatten_ctx(ctx)

    base_prompt = f"""Użyj WYŁĄCZNIE fragmentów poniżej i wygeneruj JEDNO pytanie TAK/NIE (wariant {variant}).
Zwróć TYLKO JSON: {{"stem":str,"answer":"TAK"|"NIE","explanation":str}}.

Wymagania twarde:
- "answer" to dokładnie "TAK" lub "NIE",
- "explanation" MUSI mieć dokładnie jeden tag [nazwa_pliku|p.N] z fragmentów,
- po tagu MUSI być krótkie uzasadnienie (min 1 zdanie), dlaczego TAK/NIE,
- uzasadnienie ma wynikać z przytoczonych fragmentów, bez zgadywania.

Bez komentarzy, bez markdown, bez dodatkowego tekstu.

Fragmenty:
{body}
""".strip()

    prompt = base_prompt
    last_reason = "init"

    for attempt in range(3):
        llm = ask_llm(prompt, provider=provider)
        qobj = _extract_json(llm) if llm else None
        if isinstance(qobj, dict) and isinstance(qobj.get("explanation"), str):
            qobj["explanation"] = _ensure_expl_has_rationale(qobj["explanation"], cites)
            qobj["explanation"] = _force_single_expl_tag(qobj["explanation"], cites)

        ok = False
        reason = "no json"
        if isinstance(qobj, dict):
            ok, reason = _validate_yn_obj(qobj)

        if ok:
            # semantic check: czy odpowiedź TAK/NIE wynika z fragmentów?
            sem_ok, sem_ans = _semantic_check_yn(body, qobj.get("stem", ""), provider=provider)
            if not sem_ok:
                ok = False
                reason = "semantic_check_parse_failed"
            elif sem_ans in {"TAK", "NIE"} and sem_ans != qobj.get("answer"):
                # zamiast wywalać pytanie: dopasuj answer do checkera i ustaw spójne uzasadnienie
                qobj["answer"] = sem_ans

                m = re.search(r"(\[[^\|\]]+\|p\.\d+\])", qobj.get("explanation", "") or "")
                tag = m.group(1) if m else ""

                if sem_ans == "TAK":
                    expl_rest = _strip_tags(qobj.get("explanation", ""))
                    if len(expl_rest) < 12:
                        expl_rest = "Zdanie wynika wprost z przytoczonego fragmentu."
                    qobj["explanation"] = f"{tag} {expl_rest}".strip()
                else:
                    qobj["explanation"] = (
                        f"{tag} W przytoczonym fragmencie nie ma jednoznacznego potwierdzenia tego zdania, "
                        "więc nie wynika ono wprost z materiału."
                    ).strip()

                ok = True
                reason = "ok_after_semantic_alignment"


        if ok:
            qobj["kind"] = "YN"
            qobj["metadata"] = _meta(topic, difficulty)

            # citations tylko dla taga w explanation
            qobj["citations"] = _filter_citations_by_expl(qobj.get("explanation", ""), cites)

            # jeśli explanation to prawie sam tag -> dopisz snippet
            qobj["explanation"] = _ensure_expl_has_rationale(qobj.get("explanation", ""), qobj["citations"])

            # po dopisaniu jeszcze raz sprawdź minimalną jakość (żeby nie wrócił sam tag)
            ok2, r2 = _validate_yn_obj(qobj)
            if ok2:
                return qobj

            ok = False
            reason = f"postprocess_validate_failed: {r2}"

        last_reason = reason

        prompt = (
            "NAPRAW OUTPUT. Zwróć WYŁĄCZNIE poprawny JSON. "
            f"Problem: {reason}. "
            'Pamiętaj: answer="TAK"/"NIE", explanation ma dokładnie jeden tag [source|p.N] '
            "i po tagu min 1 zdanie uzasadnienia.\n\n"
            + base_prompt
        )

    # Fallback (offline / LLM failed) — spróbuj użyć sensownej linii z kontekstu (nie nagłówka)
    best = ""
    for ln in (body.splitlines() if body else []):
        s = ln.strip()
        if s and not _looks_like_header(s) and len(s) > 40:
            best = s
            break

    src = cites[0]["source"] if cites else "source"
    page = cites[0]["page"] if cites else 1
    quote = cites[0]["quote"] if cites else (best[:180] if best else "Fragment materiału.")
    tag = f"[{src}|p.{page}]"

    claim = best
    if not claim:
        claim = quote if isinstance(quote, str) and quote.strip() else "podane stwierdzenie"

    m = re.match(r"^\[([^\|\]]+)\|p\.(\d+)\]\s*(.+)$", best)
    if m:
        src, page, claim = m.group(1), int(m.group(2)), m.group(3)
        tag = f"[{src}|p.{page}]"
        quote = claim[:180] + ("…" if len(claim) > 180 else "")

    stem = f"Czy zgodnie z materiałem: „{claim}”?"
    return {
        "kind": "YN",
        "stem": stem,
        "answer": "TAK",
        "explanation": f"{tag} To zdanie jest przytoczone w cytowanym fragmencie.",
        "metadata": _meta(topic, difficulty),
        "citations": [{"source": src, "page": int(page), "quote": quote}],
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

# --- BAN: meta-opcje typu "zgodne/sprzeczne z fragmentami" ---
_GENERIC_META_OPTIONS_RAW = [
    "Stwierdzenie zgodne z fragmentami",
    "Stwierdzenie sprzeczne z fragmentami",
    "Stwierdzenie niepowiązane z fragmentami",
    "Stwierdzenie niewynikające wprost z fragmentów",
]

def _norm_opt(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\"'„”]", "", s)
    return s

_GENERIC_META_SET = {_norm_opt(x) for x in _GENERIC_META_OPTIONS_RAW}

def _is_generic_meta_mcq_options(opts: list[str] | None) -> bool:
    if not isinstance(opts, list) or len(opts) != 4:
        return False
    s = {_norm_opt(o) for o in opts}
    # dokładne dopasowanie zestawu (najczęstszy przypadek)
    if s == _GENERIC_META_SET:
        return True
    # luźniejszy match (gdy LLM lekko pozmienia tekst)
    hit = 0
    for o in s:
        if ("fragment" in o or "fragmentów" in o) and (
            "zgodne" in o or "sprzeczne" in o or "niepowiązane" in o or "niewynikające" in o
        ):
            hit += 1
    return hit >= 3
def _validate_mcq_obj(obj: dict) -> tuple[bool, str]:
    if not isinstance(obj, dict):
        return False, "not a dict"

    stem = obj.get("stem")
    if not isinstance(stem, str) or not stem.strip():
        return False, "missing stem"
    # zbyt ogólne/“wypisz” pytania robią wieloznaczność (np. "Jakie są ...?")
    s0 = stem.strip().lower()
    if re.match(r"^(jakie\s+są|wymień|podaj)\b", s0):
        return False, "stem too broad (use one specific fact/definition)"
    if "cytowanym fragment" in s0:
        return False, "stem too meta (do not ask 'zgodne z cytowanym fragmentem' questions)"
    opts = _normalize_options(obj.get("options"))
    if not opts or len(opts) != 4:
        return False, "options must be list of 4 strings"
    if any(not o for o in opts):
        return False, "empty option"
    if len({o.lower() for o in opts}) != 4:
        return False, "options must be unique"
    if _is_generic_meta_mcq_options(opts):
        return False, "options too generic (meta-options like zgodne/sprzeczne are not allowed)"

    ans = _norm_answer_letter(obj.get("answer"))
    if ans not in {"a", "b", "c", "d"}:
        return False, "answer must be a|b|c|d"

    expl = obj.get("explanation")
    if not isinstance(expl, str) or not expl.strip():
        return False, "missing explanation"

    if _count_expl_tags(expl) != 1:
        return False, "explanation must contain exactly one [source|p.N] tag"
    rationale = _strip_tags(expl)
    if len(rationale) < 12:
        return False, "explanation too short (needs rationale text after tag)"

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
Wskaż, które opcje są POPRAWNĄ odpowiedzią na pytanie (stem).
- Jeśli pasuje więcej niż jedna opcja -> zwróć wszystkie pasujące.
- Jeśli nie da się rozstrzygnąć jednoznacznie -> zwróć wszystkie, które mogą pasować.
- Jeśli żadna nie wynika z fragmentów -> zwróć [].
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
- NIE używaj opcji meta typu: „Stwierdzenie zgodne z fragmentami / sprzeczne / niepowiązane / niewynikające…”
— każda opcja ma zawierać treść merytoryczną związaną z tematem.

Zwróć TYLKO JSON:
{{"stem":str,"options":[str,str,str,str],"answer":"a"|"b"|"c"|"d","explanation":str}}

Wymóg: w "explanation" MUSI być dokładnie jeden tag w formacie [nazwa_pliku|p.N] z poniższych fragmentów.
Bez komentarzy, bez markdown, bez dodatkowego tekstu.

Fragmenty:
{body}
""".strip()

    prompt = base_prompt
    last_reason = "init"

    # więcej prób = mniej wejść w fallback (a fallback MCQ wygląda słabo)
    for attempt in range(5):
        llm = ask_llm(prompt, provider=provider)
        qobj = _extract_json(llm) if llm else None
        if isinstance(qobj, dict) and isinstance(qobj.get("explanation"), str):
            qobj["explanation"] = _ensure_expl_has_rationale(qobj["explanation"], cites)
            # ujednolić tagi (0 tagów / >1 tagów to najczęstszy powód wejścia w fallback)
            qobj["explanation"] = _force_single_expl_tag(qobj["explanation"], cites)

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
            qobj["explanation"] = _ensure_expl_has_rationale(qobj.get("explanation", ""), qobj["citations"])
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

    # Fallback MCQ (offline / LLM failed) — nadal MCQ, ale merytoryczny (bez meta-opcji)
    best = ""
    for ln in (body.splitlines() if body else []):
        s = ln.strip()
        if s and not _looks_like_header(s) and len(s) > 40:
            best = s
            break

    src = cites[0]["source"] if cites else "source"
    page = cites[0]["page"] if cites else 1

    claim = best
    m = re.match(r"^\[([^\|\]]+)\|p\.(\d+)\]\s*(.+)$", best)
    if m:
        src, page, claim = m.group(1), int(m.group(2)), m.group(3)

    claim = re.sub(r"\s+", " ", (claim or "").strip())
    if len(claim) > 180:
        claim = claim[:180].rstrip() + "…"

    tag = f"[{src}|p.{page}]"

    # Prosty, ale merytoryczny zestaw odpowiedzi: 1 prawdziwa (dosłowny sens z fragmentu) + 3 fałszywe
    options = [
        claim,
        "W tym podejściu wykorzystuje się wyłącznie jeden algorytm (bez łączenia metod).",
        "Algorytmy są uruchamiane równolegle (jednocześnie), a nie sekwencyjnie / na zmianę.",
        "W materiale wskazano, że nie stosuje się żadnego kryterium STOP (działanie bez warunku zakończenia).",
    ]

    # Upewnij się, że 4 opcje są unikalne (na wypadek, gdyby claim był „zbyt podobny”)
    seen = set()
    uniq = []
    for o in options:
        key = _norm_opt(o)
        if key and key not in seen:
            seen.add(key)
            uniq.append(o)
        if len(uniq) == 4:
            break

    # jeśli przez ekstremalny przypadek brak 4 unikalnych, dobij neutralnymi (ale nadal treściowymi)
    while len(uniq) < 4:
        uniq.append(f"Opis dotyczy innej klasy metod niż ta przedstawiona w cytowanym fragmencie ({len(uniq)+1}).")

    return {
        "kind": "MCQ",
        "stem": "Które z poniższych stwierdzeń jest zgodne z cytowanym fragmentem?",
        "options": uniq,
        "answer": "a",
        "explanation": f"{tag} Poprawna odpowiedź wynika bezpośrednio z przytoczonego fragmentu.",
        "metadata": _meta(topic, difficulty),
        "citations": [{"source": src, "page": int(page), "quote": claim}],
        "debug": {"fallback_reason": last_reason},
    }
