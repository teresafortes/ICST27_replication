#!/usr/bin/env python3
"""
check_model_parity.py  —  Do agent runs send the same model settings as the other modes?

Sends the same three-turn conversation for every model in MODELS twice, to a local
fake endpoint (no real API calls):
  call_model  1-run_evaluation.call_model, as single-shot and iterative use it (run with
              the system python3), with the last turn marked for prompt caching for
              Claude, as the agent runner does
  agent       mini-SWE-agent's LitellmTextbasedModel with 1c-run_agent.litellm_settings
              (run with the agent venv)
and prints the differences between the recorded request bodies.

Expected on 2026-10-24 (mini-swe-agent 2.4.6, litellm 1.106.2): settings identical
for all three models; LiteLLM sends Claude message text as a one-element list of
text blocks instead of a string (same content), and adds "usage": {"include": true}
for OpenRouter (usage accounting only) plus its attribution headers; call_model
streams Claude calls ("stream": true), LiteLLM doesn't.

Usage (from the repo root):
  iii-evaluation/.venv-agent/bin/python iii-evaluation/agent_sandbox/check_model_parity.py
"""

import importlib.util, json, os, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EVAL = Path(__file__).resolve().parent.parent
MESSAGES = [{"role": "user", "content": "TASK"},
            {"role": "assistant", "content": "THOUGHT\n```bash\nls\n```"},
            {"role": "user", "content": "program.dfy"}]
RECORDS = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        RECORDS.append(body)
        text = "THOUGHT\n\n```bash\nls\n```"
        if "/messages" in self.path:
            out = {"id": "m", "type": "message", "role": "assistant", "model": body.get("model"),
                   "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                   "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
        else:
            out = {"id": "c", "object": "chat.completion", "created": 0, "model": body.get("model"),
                   "choices": [{"index": 0, "finish_reason": "stop",
                                "message": {"role": "assistant", "content": text}}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        if body.get("stream") and "/messages" in self.path:      # Anthropic streaming (SSE)
            msg = dict(out, content=[], usage={"input_tokens": 1, "output_tokens": 0})
            events = [("message_start", {"type": "message_start", "message": msg}),
                      ("content_block_start", {"type": "content_block_start", "index": 0,
                                               "content_block": {"type": "text", "text": ""}}),
                      ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": text}}),
                      ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                      ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 1},
                                         "delta": {"stop_reason": "end_turn", "stop_sequence": None}}),
                      ("message_stop", {"type": "message_stop"})]
            data = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()
            ctype = "text/event-stream"
        else:
            data, ctype = json.dumps(out).encode(), "application/json"
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}"


def load(name, path):
    sys.path.insert(0, str(EVAL))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def side_call_model():
    r = load("run_evaluation", EVAL / "1-run_evaluation.py")
    base, out = serve(), {}
    for name, cfg in r.MODELS.items():
        if cfg["client"] == "anthropic":
            client = r.anthropic.Anthropic(api_key="x", base_url=base)
            msgs = [dict(m) for m in MESSAGES]
            msgs[-1]["content"] = [{"type": "text", "text": MESSAGES[-1]["content"],
                                    "cache_control": {"type": "ephemeral"}}]
        else:
            client, msgs = r.openai.OpenAI(api_key="x", base_url=base + "/v1"), MESSAGES
        RECORDS.clear()
        r.call_model(client, cfg, msgs)
        out[name] = RECORDS[-1]
    return out


def side_agent():
    os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
    for k in ("API_KEY_ANTHROPIC_ICST", "API_KEY_OPEN_ROUTER_ICST"):
        os.environ.setdefault(k, "x")
    agent = load("run_agent", EVAL / "1c-run_agent.py")
    from minisweagent.models.litellm_textbased_model import LitellmTextbasedModel
    import litellm
    litellm.suppress_debug_info = True
    base, out = serve(), {}
    for name, cfg in agent.runner.MODELS.items():
        st = agent.litellm_settings(name)
        st["model_kwargs"] = dict(st["model_kwargs"], api_base=base + ("" if cfg["client"] == "anthropic" else "/v1"))
        RECORDS.clear()
        LitellmTextbasedModel(**st).query(MESSAGES)
        out[name] = RECORDS[-1]
    return out


def as_text(content):
    """Message content as plain text, whether a string or a list of text blocks."""
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content)
    return content


def diff(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                yield f"{path}{k}: only in agent request = {json.dumps(b[k])}"
            elif k not in b:
                yield f"{path}{k}: only in call_model request = {json.dumps(a[k])}"
            else:
                yield from diff(a[k], b[k], f"{path}{k}.")
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            yield from diff(x, y, f"{path}{i}.")
    elif a != b:
        yield f"{path[:-1]}: call_model={json.dumps(a)[:120]} agent={json.dumps(b)[:120]}"


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--side":
        print(json.dumps(side_call_model() if sys.argv[2] == "call_model" else side_agent()))
        return
    system_py = os.environ.get("SYSTEM_PYTHON", "python3")
    ref = json.loads(subprocess.run([system_py, __file__, "--side", "call_model"], check=True,
                                    capture_output=True, text=True).stdout.splitlines()[-1])
    new = side_agent()
    ok = True
    for model in ref:
        a, b = ref[model], new[model]
        settings = [d for d in diff({k: v for k, v in a.items() if k != "messages"},
                                    {k: v for k, v in b.items() if k != "messages"})]
        same_text = ([(m["role"], as_text(m["content"])) for m in a["messages"]] ==
                     [(m["role"], as_text(m["content"])) for m in b["messages"]])
        fmt = [d for d in diff(a["messages"], b["messages"])]
        print(f"\n== {model}")
        print("  settings: " + ("identical" if not settings else "DIFFERENT"))
        for d in settings:
            print("    " + d)
        print("  message text: " + ("identical" if same_text else "DIFFERENT"))
        if fmt and same_text:
            print("  message format differs (same text): e.g. " + fmt[0])
        # "stream": call_model streams Claude calls (the SDK requires it for a large max_tokens);
        # LiteLLM doesn't. Transport only: same request otherwise.
        ok &= same_text and all(d.startswith("usage: only in agent") or d.startswith("stream: only in call_model")
                                for d in settings)
    print("\nPARITY OK (only the known LiteLLM additions)" if ok else "\nPARITY PROBLEM: see above")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
