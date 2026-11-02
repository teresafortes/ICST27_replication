#!/usr/bin/env python3
"""
1d-run_claude_code.py  —  Agentic condition ag_main_cc: Claude Code, fully agentic
(Opus 5.5 only; added 2026-10-26).

What differs from ag_main (1c-run_agent.py, mini-SWE-agent)
  Harness   Claude Code (version pinned in agent_sandbox_cc/Dockerfile) in print mode,
            with its own system prompt and tools; prompts.AGENT_CC_APPEND is appended.
  Dafny     Installed in the sandbox (same Dafny 4.11 / Z3 4.15.4 as the host); the agent
            runs it as often as it likes. No `verify`/`resolve` commands, no verify cap.
  Limits    Only safety limits: --max-turns (default 100), --time-limit (1800 s) and
            --max-budget (USD per conversation, Claude Code's own cap).
  Task      prompts.build_agent_task(item, "ag_main_cc"): ag_main's task with steps 3-4
            naming Dafny instead of `verify` (prompts.AGENT_TASK_CC).
  Same      Model and effort (claude-opus-5-5, effort high), the Dafny reference at the
            start of the task, diagnosis.json, one fresh container per conversation,
            scoring (1c-run_agent.py's score(), run with --score here).

Sandbox (agent_sandbox_cc/)
  One container per conversation on the Docker network cc-internal, created with
  --internal (no route out). Its only exit is the proxy container cc-egress
  (egress_proxy.py), which allows CONNECT to api.anthropic.com:443 and denies the rest, so
  Claude Code reaches the API but neither it nor its shell can reach the web (the
  benchmark's programs are public; the original could otherwise be looked up).
  WebFetch and WebSearch are also denied in the image's managed settings, which the agent
  can't override. Every proxy request (allowed or denied) is logged and the lines for the
  conversation's container are saved in its response file (egress).
  A PostToolUse hook (managed settings) snapshots program.dfy and diagnosis.json after
  every tool call, which gives first_edit_step and first_diagnosis_step as in ag_main.
  The container runs as the host user with memory/CPU/process limits.

Output: responses/ag_main_cc/claude-opus-5-5/{tier}_{task}_{stem}_run{r}.json, in the
ag_main format (steps with kind, verify_calls = Dafny verify runs found in Bash commands,
tokens, final program and diagnosis.json, final_pass checked on the host) plus
cost_usd (Claude Code's total_cost_usd), num_turns and egress. Claude Code's full
stream-json transcript is saved to logs/agent_traj/ag_main_cc/{model}/{name}.jsonl.

Statuses: finished (Claude Code ended the session itself), turn_limit, budget_limit,
time_limit, error (no result from Claude Code). final_pass is the repair outcome.

Usage
  docker build -t dafny-agent-sandbox-cc:1 iii-evaluation/agent_sandbox_cc
  python3 iii-evaluation/1d-run_claude_code.py --subset 200 --workers 4
  python3 iii-evaluation/1d-run_claude_code.py --score
Testing without API calls: scratchpad agent_selftest_cc (a mock Anthropic API container,
passed with --mock-url).
"""

from __future__ import annotations
import argparse, importlib.util, json, logging, os, re, shutil, subprocess, sys, tempfile, time, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))
import prompts


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod      # scoring's worker processes pickle functions by module name
    spec.loader.exec_module(mod)
    return mod

agent = _load("run_agent", EVAL_DIR / "1c-run_agent.py")   # state, saving, scoring, host Dafny
runner = agent.runner

CONDITION = "ag_main_cc"
MODEL     = "claude-opus-5-5"
IMAGE     = "dafny-agent-sandbox-cc:1"
NETWORK   = "cc-internal"
PROXY     = "cc-egress"
PROXY_URL = f"http://{PROXY}:3128"
API_HOST  = "api.anthropic.com:443"
EFFORT    = "high"          # same effort as every other mode (runner.MODELS)
DAFNY_CMD = re.compile(r"(^|[\s;&|(/])dafny\b")
DAFNY_OK  = re.compile(r"verifier finished with \d+ verified, 0 errors")
TOOL_KIND = {"Write": "edit", "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
             "Read": "read", "Grep": "read", "Glob": "read", "LS": "read",
             "Task": "subagent", "Agent": "subagent", "TodoWrite": "todo"}


def docker(*args, **kw):
    return subprocess.run([DOCKER, *args], capture_output=True, text=True, **kw)


# ─── infrastructure: internal network and egress proxy ───────────────────────

def ensure_infra(image: str, allow: str):
    """(Re)create the internal network and the proxy, so the proxy's allow list is this run's."""
    if docker("network", "inspect", NETWORK).returncode:
        r = docker("network", "create", "--internal", NETWORK)
        if r.returncode:
            sys.exit(f"could not create network {NETWORK}: {r.stderr}")
    docker("rm", "-f", PROXY)
    r = docker("run", "-d", "--name", PROXY, "--network", "bridge", "-e", f"ALLOW={allow}",
               "--memory", "256m", image, "python3", "-u", "/opt/cc/egress_proxy.py")
    if r.returncode:
        sys.exit(f"could not start the egress proxy: {r.stderr}")
    r = docker("network", "connect", NETWORK, PROXY)
    if r.returncode:
        sys.exit(f"could not attach the proxy to {NETWORK}: {r.stderr}")


def egress_lines(ip: str, since: str) -> list[str]:
    out = docker("logs", "--since", since, PROXY)
    return [l for l in (out.stdout + out.stderr).splitlines() if f" {ip} " in l]


# ─── transcript parsing ──────────────────────────────────────────────────────

def tool_output(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, list):
        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c or ""


def step_kind(name: str, inp: dict) -> str:
    if name == "Bash":
        cmd = inp.get("command", "")
        if DAFNY_CMD.search(cmd):
            return ("dafny_verify" if re.search(r"\bverify\b", cmd) else
                    "dafny_resolve" if re.search(r"\bresolve\b", cmd) else "dafny_other")
        return "command"
    return TOOL_KIND.get(name, name.lower())


def apply_transcript(state: dict, events: list[dict], snap_dir: Path, item: dict):
    """Fill steps, verify_calls, tokens and status from Claude Code's stream-json events."""
    steps, by_id, texts = [], {}, []
    for ev in events:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            state["harness"].update(version=ev.get("claude_code_version") or state["harness"]["version"],
                                    session_model=ev.get("model"), tools=ev.get("tools"),
                                    permission_mode=ev.get("permissionMode"))
        elif t == "assistant":
            for b in ev.get("message", {}).get("content", []):
                if b.get("type") == "text" and b.get("text", "").strip():
                    texts.append({"after_step": len(steps), "text": b["text"],
                                  "subagent": ev.get("parent_tool_use_id") is not None})
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    rec = {"step": len(steps) + 1, "tool": b.get("name"), "input": inp,
                           "tool_use_id": b.get("id"), "parent_tool_use_id": ev.get("parent_tool_use_id"),
                           "kind": step_kind(b.get("name", ""), inp),
                           "command": inp.get("command") if b.get("name") == "Bash" else None,
                           "output": None, "is_error": None, "program_changed": False,
                           "diagnosis_changed": False, "program": None, "diagnosis_json": None}
                    steps.append(rec)
                    by_id[rec["tool_use_id"]] = rec
        elif t == "user":
            content = ev.get("message", {}).get("content", [])
            for b in content if isinstance(content, list) else []:
                if b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                    rec = by_id[b["tool_use_id"]]
                    rec["output"], rec["is_error"] = agent.truncate(tool_output(b)), bool(b.get("is_error"))
        elif t == "result":
            state["result"] = {k: ev.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms",
                                                      "duration_api_ms", "total_cost_usd", "stop_reason")}
            state["result_text"] = ev.get("result")
            u = ev.get("usage") or {}
            read_ = u.get("cache_read_input_tokens") or 0
            state["tokens"] = {"input": (u.get("input_tokens") or 0) + read_
                                        + (u.get("cache_creation_input_tokens") or 0),
                               "output": u.get("output_tokens") or 0}
            state["cached_input_tokens"] = read_
            state["usage"] = u
            state["model_usage"] = ev.get("modelUsage")
            state["cost_usd"] = ev.get("total_cost_usd")
            state["num_turns"] = ev.get("num_turns")

    # file snapshots from the PostToolUse hook, in the order the tools finished
    snaps = {}
    for f in sorted(snap_dir.glob("*.json")):
        try:
            s = json.loads(f.read_text())
            snaps[s.get("tool_use_id")] = s
        except (OSError, json.JSONDecodeError):
            pass
    last_prog, last_diag = item["source"], None
    for rec in steps:
        s = snaps.get(rec["tool_use_id"])
        if not s:
            continue
        if s["program"] != last_prog:
            rec["program_changed"], rec["program"], last_prog = True, s["program"], s["program"]
            if state["first_edit_step"] is None:
                state["first_edit_step"] = rec["step"]
        if s["diagnosis_json"] != last_diag:
            rec["diagnosis_changed"], rec["diagnosis_json"], last_diag = True, s["diagnosis_json"], s["diagnosis_json"]
            if state["first_diagnosis_step"] is None and s["diagnosis_json"] is not None:
                state["first_diagnosis_step"] = rec["step"]

    state["steps"] = steps
    state["assistant_text"] = texts
    state["verify_calls"] = [{"step": r["step"], "command": r["command"], "program": r["program"] or last_prog,
                              "pass": bool(r["output"] and DAFNY_OK.search(r["output"])),
                              "exit_code": None, "timeout": False}
                             for r in steps if r["kind"] == "dafny_verify"]
    state["resolve_calls"] = [{"step": r["step"], "command": r["command"]}
                              for r in steps if r["kind"] == "dafny_resolve"]


STATUS = {"success": "finished", "error_max_turns": "turn_limit", "error_max_budget_usd": "budget_limit"}


# ─── one conversation ────────────────────────────────────────────────────────

def run_conversation(state: dict, item: dict, args, logger) -> dict:
    ws, snap = Path(tempfile.mkdtemp(prefix="ws_")), Path(tempfile.mkdtemp(prefix="snap_"))
    (ws / "program.dfy").write_text(item["source"])
    name = f"cc_{uuid.uuid4().hex[:12]}"
    traj = agent.TRAJ_DIR / CONDITION / MODEL / agent.conv_path(CONDITION, MODEL, item, state["run_index"]).with_suffix(".jsonl").name
    traj.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env_args = ["-e", "HOME=/home/agent", "-e", f"HTTPS_PROXY={PROXY_URL}", "-e", f"https_proxy={PROXY_URL}",
                "-e", f"HTTP_PROXY={PROXY_URL}", "-e", f"http_proxy={PROXY_URL}",
                "-e", "DISABLE_AUTOUPDATER=1", "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
                "-e", "DISABLE_TELEMETRY=1", "-e", "DISABLE_ERROR_REPORTING=1", "-e", "ANTHROPIC_API_KEY"]
    if args.mock_url:
        host = re.sub(r"^https?://([^:/]+).*", r"\1", args.mock_url)
        env["ANTHROPIC_API_KEY"] = "mock-key"
        env_args += ["-e", f"ANTHROPIC_BASE_URL={args.mock_url}", "-e", f"NO_PROXY={host}", "-e", f"no_proxy={host}"]
    else:
        env["ANTHROPIC_API_KEY"] = os.environ[runner.MODELS[MODEL]["api_key_env"]]   # value never in argv
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # Z: docker reads a bare time as local
    t_start = time.time()
    try:
        r = subprocess.run([DOCKER, "run", "-d", "--rm", "--name", name, "--network", NETWORK,
                            "--user", f"{os.getuid()}:{os.getgid()}", "--memory", "4g", "--cpus", str(args.cpus),
                            "--pids-limit", "512", "-v", f"{ws}:/work", "-v", f"{snap}:/snap", *env_args,
                            args.image, "sleep", str(args.time_limit + 600)],
                           capture_output=True, text=True, env=env)
        if r.returncode:
            raise RuntimeError(f"docker run failed: {r.stderr.strip()}")
        ip = docker("inspect", "-f", f"{{{{(index .NetworkSettings.Networks \"{NETWORK}\").IPAddress}}}}",
                    name).stdout.strip()
        cmd = [DOCKER, "exec", "-i", "-w", "/work", name, "claude", "-p",
               "--model", runner.MODELS[MODEL]["model_id"], "--effort", args.effort,
               "--output-format", "stream-json", "--verbose", "--no-session-persistence",
               "--max-turns", str(args.max_turns), "--max-budget-usd", str(args.max_budget),
               "--dangerously-skip-permissions", "--append-system-prompt", prompts.AGENT_CC_APPEND,
               "--disallowedTools", "WebFetch", "WebSearch"]
        task = prompts.build_agent_task(item, CONDITION)
        state["messages"] = [{"role": "system", "content": prompts.AGENT_CC_APPEND},
                             {"role": "user", "content": task}]
        state["harness"] = {"name": "claude-code", "version": None, "sandbox": "docker", "image": args.image,
                            "model": runner.MODELS[MODEL]["model_id"], "effort": args.effort,
                            "mock": bool(args.mock_url)}
        timed_out = False
        with open(traj, "w") as out:
            try:
                subprocess.run(cmd, input=task, stdout=out, stderr=subprocess.PIPE, text=True,
                               timeout=args.time_limit)
            except subprocess.TimeoutExpired:
                timed_out = True
                docker("kill", name)
        events = []
        for line in traj.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        apply_transcript(state, events, snap, item)
        res = state.get("result")
        # Claude Code reports API failures (e.g. "Credit balance is too low") as subtype
        # success with is_error; those never reached the model, so they are errors
        state["status"] = ("time_limit" if timed_out else "error" if res is None or res.get("is_error")
                           else STATUS.get(res["subtype"], res["subtype"]))
        state["egress"] = egress_lines(ip, since) if ip else []
        state["final_program"] = agent.read(ws / "program.dfy")
        state["final_diagnosis_json"] = agent.read(ws / "diagnosis.json")
        _, code = agent.iterative.run_dafny(state["final_program"] or "", args.timeout)
        state["final_pass"] = code == 0
        state["wall_s"] = round(time.time() - t_start, 1)
        state["finished"] = datetime.now(timezone.utc).isoformat()
        agent.save(state)
        denied = sum(" DENY " in l for l in state["egress"])
        logger.info(f"{state['item_id']} run{state['run_index']}: {state['status']} turns={state.get('num_turns')} "
                    f"tools={len(state['steps'])} dafny_verify={len(state['verify_calls'])} "
                    f"cost={state.get('cost_usd')} denied={denied} pass={state['final_pass']}")
    finally:
        docker("rm", "-f", name)
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(snap, ignore_errors=True)
    return state


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    global DOCKER
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--image", default=IMAGE)
    ap.add_argument("--docker", default=None)
    ap.add_argument("--workers", type=int, default=4, help="Parallel conversations (each runs Dafny in its container)")
    ap.add_argument("--cpus", type=float, default=2, help="CPUs per container")
    ap.add_argument("--effort", default=EFFORT)
    ap.add_argument("--max-turns", type=int, default=100, help="Safety limit (Claude Code --max-turns)")
    ap.add_argument("--max-budget", type=float, default=3.0, help="Safety limit, USD per conversation")
    ap.add_argument("--time-limit", type=int, default=1800, help="Wall seconds per conversation")
    ap.add_argument("--timeout", type=int, default=20, help="Dafny time limit for the host-side final check")
    ap.add_argument("--subset", type=int, choices=runner.MODE_SUBSET_SIZES)
    ap.add_argument("--limit", type=int, help="Stratified sample of N items; smoke tests")
    ap.add_argument("--items", nargs="+")
    ap.add_argument("--mock-url", help="Testing: base URL of a mock Anthropic API on the cc-internal network")
    ap.add_argument("--responses-dir", help="Testing: write responses/trajectories/scores under this directory")
    ap.add_argument("--score", action="store_true", help="Score saved conversations (1c-run_agent.py's scoring); no API calls")
    args = ap.parse_args()

    if args.responses_dir:
        base = Path(args.responses_dir)
        agent.RESP_DIR, agent.TRAJ_DIR, agent.SCORES = base / "responses", base / "agent_traj", base / "scores"
    if args.score:
        agent.score(CONDITION, 4, 15)
        return
    if args.docker:
        os.environ["DOCKER"] = args.docker
    DOCKER = agent.find_docker()
    if DOCKER is None:
        sys.exit("docker not found")
    if docker("image", "inspect", args.image).returncode:
        sys.exit(f"Image {args.image} not found: docker build -t {args.image} iii-evaluation/agent_sandbox_cc")
    if not args.mock_url and not os.environ.get(runner.MODELS[MODEL]["api_key_env"]):
        sys.exit(f"{runner.MODELS[MODEL]['api_key_env']} not set")
    if not (args.subset or args.limit or args.items):
        sys.exit("Pass --subset N (main run), or --limit N / --items (smoke test).")

    items = json.loads(agent.BENCHMARK.read_text())["items"]
    if args.subset:
        items = runner.mode_subset(items, args.subset)
    if args.items:
        items = [i for i in items if i["item_id"] in set(args.items)]
    if args.limit:
        items = runner.stratified_sample(items, args.limit)

    ensure_infra(args.image, API_HOST)
    limits = {"turns": args.max_turns, "seconds": args.time_limit, "budget_usd": args.max_budget,
              "verify": None, "steps": None, "tokens": None}
    log_dir = agent.LOG_DIR if not args.responses_dir else Path(args.responses_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(log_dir / f"{MODEL}_{CONDITION}.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", force=True)
    logger = logging.getLogger(MODEL)
    todo = []
    for item in items:
        for r in range(1, args.runs + 1):
            p = agent.conv_path(CONDITION, MODEL, item, r)
            if p.exists() and json.loads(p.read_text())["status"] != "active":
                continue
            todo.append((agent.new_state(CONDITION, MODEL, item, r, limits), item))
    print(f"[{MODEL}] {len(todo)} conversations to run ({len(items) * args.runs - len(todo)} done) "
          f"→ {agent.RESP_DIR / CONDITION / MODEL}/" + ("  [MOCK API]" if args.mock_url else ""))
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(run_conversation, s, it, args, logger) for s, it in todo]
            for n, fut in enumerate(as_completed(futs), 1):
                try:
                    s = fut.result()
                    print(f"  [{n}/{len(todo)}] {s['item_id']}: {s['status']}, {s.get('num_turns')} turns, "
                          f"{len(s['verify_calls'])} dafny verify, ${s.get('cost_usd') or 0:.3f}, pass={s['final_pass']}")
                except Exception as e:
                    logger.exception("conversation failed")
                    print(f"  [{n}/{len(todo)}] FAILED: {e}")
    finally:
        (log_dir / "cc_egress.log").open("a").write(docker("logs", PROXY).stdout)
        docker("rm", "-f", PROXY)


DOCKER = None

if __name__ == "__main__":
    main()
