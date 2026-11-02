"""
leak_judge.py  —  API client for the intent leak judge (3-build_intent_descriptions.py --judge).

The judge configuration used to live in iii-evaluation/5-run_judge.py, which was removed
on 2026-10-25 when the explanation judges were replaced by rule-based codes; this keeps
only what the leak judge needs (same model and settings as before).
"""
from __future__ import annotations
import json, os, re, time

JUDGE_MODELS = {
    "gpt-5.4": {
        "model_id":    "gpt-5.4",
        "api_key_env": "OPENAI_API_KEY_ICST",
        "extra_kwargs": {},   # gpt-5.4 supports only the default temperature (1)
    },
}


def make_client(model_key: str):
    import openai
    cfg = JUDGE_MODELS[model_key]
    key = os.environ.get(cfg["api_key_env"], "")
    if not key:
        raise SystemExit(f"{cfg['api_key_env']} is not set (needed for the {model_key} leak judge)")
    return openai.OpenAI(api_key=key), cfg


def call(client, cfg: dict, prompt: str, max_retries: int = 4) -> str:
    delay = 5
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=cfg["model_id"], messages=[{"role": "user", "content": prompt}], **cfg["extra_kwargs"])
            return resp.choices[0].message.content or ""
        except Exception:
            if attempt == max_retries - 1:
                raise
            time.sleep(delay)
            delay *= 2


def extract_json(text: str) -> dict | None:
    text = re.sub(r"```(?:json)?\s*|```", "", text or "")
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    for pattern in (r"\{[^{}]*\}", r"\{(?:[^{}]|\{[^{}]*\})*\}"):
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None
