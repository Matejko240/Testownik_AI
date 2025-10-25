Testownik AI — Backend (FastAPI + RAG)

RAG z PDF/PPTX/DOCX/EPUB → pytania TAK/NIE i ABCD z poprawną odpowiedzią, wyjaśnieniem i cytowaniem (plik + strona/slajd + cytat).

Instalacja i start
pip install -r requirements.txt
fastapi dev apps/api/main.py
# API:    http://127.0.0.1:8000
# Swagger http://127.0.0.1:8000/docs


Windows: przy uploadzie używaj curl.exe.

Szybkie testy
# 1) search (pusty indeks = pusto, to OK)
curl -X POST http://127.0.0.1:8000/search -H "Content-Type: application/json" -d "{`"query`":`"test`",`"k`":5}"

# 2) upload materiałów
curl.exe -F "files=@AI_Testownik.pdf" http://127.0.0.1:8000/upload
# przykład: podkatalog
curl.exe -F "files=@data/sources/SP-W03.pdf" http://127.0.0.1:8000/upload

# 3) generacja pytań
# ABCD
curl -X POST http://127.0.0.1:8000/gen/mcq -H "Content-Type: application/json" -d "{`"topic`":`"sterowniki programowalne`",`"difficulty`":`"medium`"}"
# TAK/NIE
curl -X POST http://127.0.0.1:8000/gen/yn  -H "Content-Type: application/json" -d "{`"topic`":`"sterowniki programowalne`"}"

# 4) ocena pytania (feedback)
curl -X POST http://127.0.0.1:8000/rate -H "Content-Type: application/json" -d "{`"question_id`":`"<ID>`",`"score`":9,`"feedback`":`"OK"`}"

Endpoints (skrót)
Method	Path	Body	Opis
POST	/upload	files=@plik (multipart, wiele plików)	Indeksacja PDF/PPTX/DOCX/EPUB.
POST	/search	{ "query": "...", "k": 8 }	Fragmenty z cytowaniami.
POST	/gen/yn	{ "topic": "...", "difficulty": "..."}	Pytanie TAK/NIE + wyjaśnienie + cytowania.
POST	/gen/mcq	{ "topic": "...", "difficulty": "..."}	Pytanie ABCD + klucz + wyjaśnienie + cytowania.
POST	/rate	{ "question_id": "...", "score": 1..10, "feedback": "..." }	Ocena pytania.
ENV (opcjonalnie)

Skopiuj .env.example → .env:

APP_HOST=127.0.0.1
APP_PORT=8000
EMB_MODEL=sentence-transformers/all-MiniLM-L6-v2
LLM_PROVIDER=none        # openai|ollama|none
OPENAI_API_KEY=
OLLAMA_BASE_URL=


LLM_PROVIDER=none działa offline (fallback).

OpenAI → zainstaluj openai==1.43.0, ustaw OPENAI_API_KEY.

Ollama → ustaw OLLAMA_BASE_URL (np. http://127.0.0.1:11434).

Struktura danych (lokalnie)
data/
  sources/   # materiały (nie commituj)
  index/     # db + embeddingi (nie commituj)

Tipy

Lepszy wynik = dopasowany topic do wgranych plików.

PowerShell: w JSON używaj backticków ` do escapingu ".

Upload ze spacjami: curl.exe -F "files=@C:/.../SP-W03.pdf" http://127.0.0.1:8000/upload