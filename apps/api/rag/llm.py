from ..settings import settings
from ..providers.base import LLMProvider

def _provider(provider_override: str | None = None) -> LLMProvider | None:
    """provider_override:
      - None / "default" -> użyj settings.llm_provider
      - "none"           -> wyłącz LLM (zwróć None)
      - "openai"|"ollama"-> wymuś
    """

    # 1) rozstrzygamy tryb
    if provider_override is None or str(provider_override).lower() == "default":
        prov_name = (settings.llm_provider or "none").lower()
    else:
        prov_name = str(provider_override).lower()

    # 2) jawne wyłączenie
    if prov_name == "none":
        return None

    # 3) wybór providera
    if prov_name == "openai" and settings.openai_api_key:
        from ..providers.openai_provider import OpenAIProvider
        return OpenAIProvider(api_key=settings.openai_api_key)

    if prov_name == "ollama" and settings.ollama_base_url:
        from ..providers.ollama_provider import OllamaProvider
        return OllamaProvider(base_url=settings.ollama_base_url)

    # jeśli nie da się zainicjalizować (brak key/url) -> traktuj jak none
    return None


def ask_llm(prompt: str, format=None, provider: str | None = None) -> str | None:
    prov = _provider(provider)
    if not prov:
        return None
    return prov.generate(prompt, format=format)
