import requests, json
from .base import LLMProvider

class OllamaProvider(LLMProvider):
    def __init__(self, base_url:str):
        self.base_url = base_url
        self.model = "llama3.1"  # lub inny lokalny
    def generate(self, prompt:str)->str:
        r = requests.post(f"{self.base_url}/api/generate", json={"model":self.model,"prompt":prompt,"stream":False})
        r.raise_for_status()
        return r.json().get("response","")
