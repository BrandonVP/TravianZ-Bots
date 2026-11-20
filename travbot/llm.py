"""One way to ask a model, for every AI call (advisor plans, AI letters, mail replies, reading members' letters).

A model name is an Ollama model ("gemma4:26b") or "strata": then the request goes to the Strata server on the NAS
(OpenAI API, the 125B Qwen3.8-Flash-Next; plan 2026-11-20-strata). The callers try their models in order, so
"strata,gemma4:26b,qwen3:14b" falls back to Ollama when Strata is down or its reply is not usable.

Environment:
  TRAVBOT_STRATA_URL       Strata server (default http://192.168.68.204:8080)
  TRAVBOT_STRATA_KEY_FILE  file holding its API key (default secrets/strata_key next to the code; never committed)
  TRAVBOT_STRATA_KEY       the key itself (instead of the file)
"""
import os
import pathlib

import requests

STRATA = "strata"
_ROOT = pathlib.Path(__file__).resolve().parent.parent


def is_strata(model):
    return model == STRATA


def uses_strata(models):
    return any(is_strata(m) for m in models)


def _strata_key():
    key = os.environ.get("TRAVBOT_STRATA_KEY", "").strip()
    if key:
        return key
    path = pathlib.Path(os.environ.get("TRAVBOT_STRATA_KEY_FILE") or _ROOT / "secrets" / "strata_key")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        raise ValueError(f"no Strata API key ({path} unreadable, TRAVBOT_STRATA_KEY unset)")


def chat(ollama_url, model, messages, schema, *, temperature, num_ctx, num_predict, timeout, keep_alive=0):
    """The model's reply text: a JSON object as text, asked to match `schema` (callers still check the fields).
    Raises requests.RequestException, ValueError or KeyError like the plain Ollama call it replaces."""
    if is_strata(model):
        return _strata(messages, schema, temperature=temperature, num_predict=num_predict, timeout=timeout)
    r = requests.post(f"{ollama_url.rstrip('/')}/api/chat", timeout=timeout, json={
        "model": model, "stream": False, "think": False, "format": schema, "keep_alive": keep_alive,
        "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict},
        "messages": messages})
    r.raise_for_status()
    return r.json()["message"]["content"]


def _strata(messages, schema, *, temperature, num_predict, timeout):
    url = os.environ.get("TRAVBOT_STRATA_URL", "http://192.168.68.204:8080").rstrip("/")
    body = {"messages": messages, "stream": False, "temperature": temperature, "max_tokens": num_predict,
            "reasoning_effort": "none",   # = Ollama's think: False
            # Strata prompts for the schema and checks the answer is one JSON object (not grammar-constrained):
            # a malformed answer comes back as 502 structured_output_failed, which is worth one more try
            "response_format": {"type": "json_schema", "json_schema": {"name": "reply", "schema": schema}}}
    headers = {"Authorization": "Bearer " + _strata_key()}
    for attempt in (1, 2):
        r = requests.post(url + "/v1/chat/completions", timeout=timeout, headers=headers, json=body)
        if attempt == 1 and r.status_code == 502 and "structured_output_failed" in r.text:
            continue
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
