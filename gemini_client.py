"""Thin wrapper around the Gemini API used for resume parsing and answering form questions."""

import json
import os
import re

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_LOCAL_BASE = "http://localhost:11434"
CONFIG_PATH = "config.json"


class GeminiError(RuntimeError):
    pass


def _load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        raise GeminiError(f"{path} not found. Create it first.")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class Gemini:
    def __init__(self, config=None):
        if isinstance(config, str):
            # Backward compatibility: a string was the Gemini model name
            full_config = _load_config()
            full_config["gemini_model"] = config
            config = full_config
        elif config is None:
            config = _load_config()

        self.config = config
        self.provider = config.get("llm_provider", "gemini")

        if self.provider == "local":
            self.base_url = config.get("local_base_url", DEFAULT_LOCAL_BASE).rstrip("/")
            self.model = config.get("local_model", "qwen2.5:14b")
            self._check_local()
        else:
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise GeminiError(
                    "GEMINI_API_KEY not set. Copy .env.example to .env and add your key."
                )
            self.client = genai.Client(api_key=api_key)
            self.model = (
                config.get("gemini_model")
                or os.getenv("GEMINI_MODEL")
                or DEFAULT_MODEL
            )

    def _check_local(self):
        if self.provider != "local":
            return
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
        except requests.RequestException as e:
            raise GeminiError(
                f"Local LLM at {self.base_url} is not reachable. Is the container running? {e}"
            )
        tags = r.json().get("models", [])
        names = {m.get("name", "") for m in tags}
        if self.model not in names and f"{self.model}:latest" not in names:
            raise GeminiError(
                f"Local model '{self.model}' is not available. "
                f"Pull it first: docker exec -it ollama ollama pull {self.model}"
            )

    def generate(self, prompt, system=None, temperature=0.2):
        if self.provider == "local":
            return self._local_generate(prompt, system=system, temperature=temperature, json=False)
        return self._gemini_generate(prompt, system=system, temperature=temperature, json=False)

    def generate_json(self, prompt, system=None, temperature=0.1):
        if self.provider == "local":
            text = self._local_generate(prompt, system=system, temperature=temperature, json=True)
        else:
            text = self._gemini_generate(prompt, system=system, temperature=temperature, json=True)
        return _parse_json(text)

    def generate_json_object(self, prompt, system=None, temperature=0.1):
        result = self.generate_json(prompt, system=system, temperature=temperature)
        if not isinstance(result, dict):
            raise GeminiError("LLM response must be a JSON object")
        return result

    def _gemini_generate(self, prompt, system=None, temperature=0.2, json=False):
        config = types.GenerateContentConfig(temperature=temperature)
        if system:
            config.system_instruction = system
        if json:
            config.response_mime_type = "application/json"
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=config,
            )
        except Exception as e:
            raise GeminiError(f"Gemini API call failed: {e}") from e
        return (response.text or "").strip()

    def _local_generate(self, prompt, system=None, temperature=0.2, json=False):
        messages = []
        if json:
            json_instr = "You must respond with ONLY a valid JSON object or array. No markdown, no explanations, no extra text."
            system = f"{system}\n\n{json_instr}" if system else json_instr
        if system:
            messages.append({"role": "system", "content": system})
        user = prompt
        if json:
            user = user + "\n\nYou must respond with ONLY valid JSON."
        messages.append({"role": "user", "content": user})
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if json:
            payload["format"] = "json"
        try:
            resp = requests.post(
                f"{self.base_url}/api/chat", json=payload, timeout=(10, None)
            )
            resp.raise_for_status()
            data = resp.json()
            text = (data.get("message", {}).get("content", "")).strip()
        except requests.RequestException as e:
            raise GeminiError(f"Local LLM call failed: {e}")
        except Exception as e:
            raise GeminiError(f"Local LLM returned an invalid response: {e}") from e
        print(f"[local LLM raw] {text[:2000]}")
        return text


def _parse_json(text):
    # Strip markdown fences if the model wraps JSON in a code block.
    stripped = re.sub(r"```(?:json)?\s*([\s\S]*?)\s*```", r"\1", text).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Model occasionally wraps JSON in a fenced block despite the mime type.
    match = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
    if not match:
        raise GeminiError(f"LLM did not return JSON:\n{text[:500]}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise GeminiError(f"LLM did not return valid JSON:\n{text[:500]}") from e
