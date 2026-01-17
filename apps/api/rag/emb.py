import numpy as np
from sentence_transformers import SentenceTransformer

_model_cache = {}

def get_model(name:str):
    if name not in _model_cache:
        _model_cache[name] = SentenceTransformer(name)
    return _model_cache[name]

def embed_texts(texts:list[str], model_name:str)->list[bytes]:
    m = get_model(model_name)
    vecs = m.encode(texts, normalize_embeddings=True)
    return [np.asarray(v, dtype=np.float32).tobytes() for v in vecs]

def embed_query(text:str, model_name:str)->np.ndarray:
    m = get_model(model_name)
    v = m.encode([text], normalize_embeddings=True)[0]
    return np.asarray(v, dtype=np.float32)
