from ..settings import settings
from ..providers.base import LLMProvider


def _provider() -> LLMProvider | None:
    if settings.llm_provider == "openai" and settings.openai_api_key:
        from ..providers.openai_provider import OpenAIProvider  # ← lazy
        return OpenAIProvider(api_key=settings.openai_api_key)

    if settings.llm_provider == "ollama" and settings.ollama_base_url:
        from ..providers.ollama_provider import OllamaProvider  # ← lazy
        return OllamaProvider(base_url=settings.ollama_base_url)

    return None

def ask_llm(prompt: str) -> str | None:
    prov = _provider()
    if not prov:
        return None
    return prov.complete(prompt)