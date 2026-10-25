"""Thin wrapper around the local Ollama HTTP API. Standard library only.

Env vars:
  OLLAMA_HOST   default http://127.0.0.1:11434
  TRIAGE_MODEL  default nemotron-3-nano:30b   (any model you pulled)
  LLM_TIMEOUT   seconds per call, default 300
  LLM_THINK     set to 0 for Qwen3-style "thinking" models (sends think=false); 1 to force on
  LLM_MOCK=1    skip the model; classify/extract return the gold labels in data/emails.json
"""
import json
import os
import re
import urllib.error
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
        "options": {"temperature": temperature, "num_predict": int(os.environ.get("LLM_MAX_TOKENS", "700"))},
    }
    if schema:
        body["format"] = schema
    think = os.environ.get("LLM_THINK", "").lower()
    if think in ("0", "false", "no"):
        body["think"] = False
    elif think in ("1", "true", "yes"):
        body["think"] = True
    last = None
    for _ in range(retries + 1):
        req = urllib.request.Request(
            HOST + "/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                out = json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 400 and "think" in body and "think" in detail.lower():
                del body["think"]  # this model has no thinking mode; retry without it
                last = RuntimeError(detail)
                continue
            raise RuntimeError("Ollama HTTP %d: %s" % (e.code, detail))
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
