# apps/api/rag/util.py
def chunk_text(text: str, max_chars: int = 1100, overlap: int = 200):
    """
    Prosty chunker: tnie tekst na kawałki o długości ~max_chars z zachodzeniem overlap.
    Używany przy budowie indeksu do RAG.
    """
    if not text:
        return []
    # normalizacja białych znaków
    text = " ".join(text.split())
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        chunks.append(text[start:end])
        if end == n:
            break
        start = max(0, end - overlap)
    return chunks
