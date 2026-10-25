"""Thin wrapper around the local Ollama HTTP API. Standard library only.

Env vars:
  OLLAMA_HOST   default http://127.0.0.1:11434
  TRIAGE_MODEL  default nemotron-3-nano:30b   (any model you pulled)
  LLM_TIMEOUT   seconds per call, default 300
  LLM_MOCK=1    skip the model; classify/extract return the gold labels in data/emails.json
"""
import json
import os
import re
import urllib.request

HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
if not HOST.startswith("http"):
    HOST = "http://" + HOST
MODEL = os.environ.get("TRIAGE_MODEL", "nemotron-3-nano:30b")
TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "300"))
MOCK = os.environ.get("LLM_MOCK") == "1"


def chat(system, user, schema=None, temperature=0.0, model=None, retries=1):
    """One chat call. With `schema` (a JSON schema dict) returns a parsed dict, else a string."""
    body = {
        "model": model or MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {"temperature": temperature},
    }
    if schema:
        body["format"] = schema
    last = None
    for _ in range(retries + 1):
        req = urllib.request.Request(
            HOST + "/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            out = json.load(r)
        txt = out["message"]["content"]
        txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
        if not schema:
            return txt
        try:
            return json.loads(txt)
        except json.JSONDecodeError as e:  # retry once
            last = e
    raise last


def ping():
    """Return the list of local model names, or raise if Ollama is unreachable."""
    with urllib.request.urlopen(HOST + "/api/tags", timeout=10) as r:
        return [m["name"] for m in json.load(r).get("models", [])]
