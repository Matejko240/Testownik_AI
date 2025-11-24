from pydantic import BaseModel
import os

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

settings = Settings()
