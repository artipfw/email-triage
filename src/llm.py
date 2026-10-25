"""Thin wrapper around the local Ollama HTTP API. Standard library only.

Env vars:
  OLLAMA_HOST     default http://127.0.0.1:11434
  TRIAGE_MODEL    default nemotron-3-nano:30b   (any model you pulled)
  LLM_TIMEOUT     seconds per call, default 300
  LLM_MAX_TOKENS  hard cap on generated tokens per call, default 400
  LLM_THINK       0 for Qwen3-style "thinking" models (sends think=false); 1 to force on
  LLM_DEBUG=1     print raw model output to stderr
  LLM_MOCK=1      skip the model; classify/extract return the gold labels in data/emails.json

Structured calls try one mode, then the other, and always return a dict matching the schema:
  constrain=True  -> Ollama grammar-constrained JSON first, then plain JSON prompting
  constrain=False -> plain JSON prompting first (robust for extraction), then constrained
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
if not HOST.startswith("http"):
    HOST = "http://" + HOST
MODEL = os.environ.get("TRIAGE_MODEL", "nemotron-3-nano:30b")
TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "300"))
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "400"))
DEBUG = os.environ.get("LLM_DEBUG") == "1"
MOCK = os.environ.get("LLM_MOCK") == "1"


def _body(system, user, temperature, model, fmt=None):
    body = {
        "model": model or MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {"temperature": temperature, "num_predict": MAX_TOKENS},
    }
    think = os.environ.get("LLM_THINK", "").lower()
    if think in ("0", "false", "no"):
        body["think"] = False
    elif think in ("1", "true", "yes"):
        body["think"] = True
    if fmt:
        body["format"] = fmt
    return body


def _post(body):
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
            return _post(body)
        raise RuntimeError("Ollama HTTP %d: %s" % (e.code, detail))
    txt = out["message"]["content"]
    return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()


def _find_json(txt):
    """First balanced {...} object in txt that parses, ignoring code fences and chatter."""
    txt = re.sub(r"```(?:json)?", "", txt)
    start = txt.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(txt)):
        c = txt[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(txt[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _coerce(schema, val):
    """Force val into the shape of schema: missing keys get defaults, types are fixed."""
    t = schema.get("type")
    if t == "object":
        val = val if isinstance(val, dict) else {}
        return {k: _coerce(sub, val.get(k)) for k, sub in schema["properties"].items()}
    if t == "array":
        return [_coerce(schema["items"], x) for x in val] if isinstance(val, list) else []
    if t == "integer":
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return 0
    if t == "number":
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0
    if t == "string":
        if "enum" in schema:
            return val if val in schema["enum"] else schema["enum"][0]
        return "" if val is None else str(val)
    return val


def chat(system, user, schema=None, temperature=0.0, model=None, constrain=True):
    """Free text if no schema; otherwise a dict that matches `schema`."""
    if not schema:
        return _post(_body(system, user, temperature, model))
    errs = []
    for use_format in ([True, False] if constrain else [False, True]):
        sys_p = system
        if not use_format:
            sys_p += ("\nReturn ONLY one JSON object, with no markdown and no commentary, "
                      "using exactly these keys and types:\n" + json.dumps(schema))
        txt = _post(_body(sys_p, user, temperature, model, schema if use_format else None))
        if DEBUG:
            print("[llm %s] %s" % ("constrained" if use_format else "plain", txt[:300]), file=sys.stderr)
        obj = _find_json(txt)
        if isinstance(obj, dict):
            return _coerce(schema, obj)
        errs.append(txt[:100].replace("\n", " "))
    raise RuntimeError("no valid JSON from model; raw: " + " | ".join(errs))


def ping():
    """Return the list of local model names, or raise if Ollama is unreachable."""
    with urllib.request.urlopen(HOST + "/api/tags", timeout=10) as r:
        return [m["name"] for m in json.load(r).get("models", [])]
