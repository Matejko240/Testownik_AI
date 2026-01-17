import requests
from ..settings import settings
from .base import LLMProvider

class OllamaProvider(LLMProvider):
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.model = settings.ollama_model

    def generate(self, prompt: str, format=None) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "think": False, 
            "options": {
                "temperature": 0.2,
                "num_predict": 800
            }
        }
        if format is not None:
            payload["format"] = format  
            
        r = requests.post(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=(5, 180)
        )

        if r.status_code != 200:
            raise RuntimeError(f"Ollama HTTP {r.status_code}: {r.text}")

        data = r.json()

        resp = (data.get("response") or "").strip()
        if not resp:
            raise RuntimeError(f"Ollama empty response. Full JSON: {data}")

        return resp
