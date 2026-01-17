from pydantic import BaseModel
import os
from dotenv import load_dotenv
load_dotenv()

class Settings(BaseModel):
    host: str = os.getenv("APP_HOST","127.0.0.1")
    port: int = int(os.getenv("APP_PORT","8000"))
    db_path: str = "data/index/testownik.db"
    index_dir: str = "data/index"
    src_dir: str = "data/sources"
    emb_model: str = os.getenv("EMB_MODEL","sentence-transformers/all-MiniLM-L6-v2")
    llm_provider: str = os.getenv("LLM_PROVIDER","none")  # openai|ollama|none
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    ollama_base_url: str | None = os.getenv("OLLAMA_BASE_URL")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen3:4b")
settings = Settings()
LLM_PROVIDER = settings.llm_provider
OLLAMA_MODEL = settings.ollama_model
print("LLM_PROVIDER =", LLM_PROVIDER)
print("OLLAMA_MODEL =", OLLAMA_MODEL)