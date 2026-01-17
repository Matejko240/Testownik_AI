import os, json
from .base import LLMProvider
from openai import OpenAI

class OpenAIProvider(LLMProvider):
    def __init__(self, api_key:str):
        self.cli = OpenAI(api_key=api_key)
    def generate(self, prompt: str, format=None) -> str:
        rsp = self.cli.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            temperature=0.2
        )
        return rsp.choices[0].message.content
