#!/usr/bin/env python3
"""
1c-run_agent.py  —  Agentic condition (ag_main), run with
mini-SWE-agent (https://mini-swe-agent.com; version pinned in agent_sandbox/requirements.txt).

Harness
  mini-SWE-agent's DefaultAgent loop with its text-based LiteLLM model
  (LitellmTextbasedModel) and its DockerEnvironment: every model turn must contain
  exactly one ```bash block; the command runs in a fresh subshell in the workspace;
  its output is the next user turn. The history is linear and nothing is summarized.
  The subclasses below only add what the study needs:
    StudyAgent   per-step records, the token limit, and plain-text history (each
                 assistant turn is kept as its answer text, without reasoning/thinking
                 blocks, exactly as in it_main)
    StudyDocker  the sandbox settings, `verify` on the host, a kill on command
                 timeout, and output truncation
  Messages: the system message is prompts.AGENT_PROTOCOL; the first user message is
  prompts.build_agent_task. Observations, format errors and verify replies use the
  prompts.AGENT_* texts; none of mini-SWE-agent's default prompt wording is used.
  As in mini-SWE-agent, an answer without exactly one bash block is not kept in the
  history; the model gets AGENT_FORMAT_ERROR instead (it still counts as a step).

Model settings
  Taken from MODELS in 1-run_evaluation.py and passed to LiteLLM unchanged, so every
  model has the same settings as in single-shot and iterative
  (agent_sandbox/check_model_parity.py compares the request bodies).

`verify`
  A command that is exactly `verify` is handled by StudyDocker, not the sandbox: it
  snapshots program.dfy and diagnosis.json, runs Dafny 4.11 on the host (20 s limit,
  counterexamples on, as in 3-apply_repairs.py) and returns the error block and
  counterexample built with the collector's build_prompt_fields, so feedback looks
  like the task prompt and like it_main. The sandbox has no Dafny, so the verify cap
  can't be bypassed. A pass ends the conversation (as in it_main).

A conversation ends with one of these statuses:
  verified      verify passed
  submitted     the agent ran `echo TASK_COMPLETE`
  verify_limit  the last allowed verify call failed
  step_limit    --step-limit model turns used
  time_limit    --time-limit seconds of wall time used
  token_limit   --token-limit total tokens used
  error         the model API failed after mini-SWE-agent's retries
After it ends, the driver verifies the final program.dfy once more (final_pass);
that is the repair outcome used in scoring, whatever the status.

Sandboxes
  docker  One container per conversation from agent_sandbox/Dockerfile, run with
          --network none, a non-root user, and memory/CPU/process limits; the
          workspace is bind-mounted at /work. Use this for the study. The docker CLI
          is found via $DOCKER, PATH, ~/.docker/bin or Docker.app.
  local   Commands run with bash in a temporary directory, with a minimal PATH.
          NOT isolated; only for testing the harness.

Output: responses/{condition}/{model}/{tier}_{task}_{stem}_run{r}.json, rewritten
after every step: messages, one record per step (command, output, exit code, whether
program.dfy / diagnosis.json changed, verify results with snapshots), the final
program and diagnosis.json. mini-SWE-agent's own trajectory is saved to
logs/agent_traj/{condition}/{model}/. A conversation left "active" by an
interruption is restarted from scratch on the next run (its workspace is not kept).

Scoring (--score, no API calls) writes scores/{condition}/:
  agent_runs.csv       one row per conversation: status, steps, verify calls, tokens,
                       final_pass, edit scope, diagnosis.json compliance and the
                       first/last diagnosis scored like 2-score_diagnosis.py
  repair_scores.csv    the same columns as other conditions (pred_repair = the clause
                       text at the mutation site in the final program)
  fidelity_scores.csv  for passing conversations, the relation of the final clause to
                       the original (4-check_semantic_equivalence.classify); edits
                       outside the mutated clause are reported in edit_scope

Usage (runs need the agent venv; --score also works with the system python3):
  /opt/homebrew/bin/python3.10 -m venv iii-evaluation/.venv-agent
  iii-evaluation/.venv-agent/bin/pip install -r iii-evaluation/agent_sandbox/requirements.txt
  docker build -t dafny-agent-sandbox:2 iii-evaluation/agent_sandbox
  iii-evaluation/.venv-agent/bin/python iii-evaluation/1c-run_agent.py --condition ag_main --limit 20
  python3 iii-evaluation/1c-run_agent.py --condition ag_main --score
"""

from __future__ import annotations
import argparse, difflib, hashlib, importlib.util, json, logging, os, re, shutil
import subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
ROOT     = EVAL_DIR.parent
sys.path.insert(0, str(EVAL_DIR))
import prompts


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

runner    = _load("run_evaluation", EVAL_DIR / "1-run_evaluation.py")
scoring   = _load("score_diagnosis", EVAL_DIR / "2-score_diagnosis.py")
iterative = _load("run_iterative", EVAL_DIR / "1b-run_iterative.py")   # run_dafny
equiv     = _load("check_equivalence", EVAL_DIR / "4-check_semantic_equivalence.py")
collector = iterative.collector

BENCHMARK = ROOT / "ii-corpus" / "benchmark.json"
CORPUS    = ROOT / "ii-corpus"
RESP_DIR  = EVAL_DIR / "responses"
SCORES    = EVAL_DIR / "scores"
LOG_DIR   = EVAL_DIR / "logs"
TRAJ_DIR  = LOG_DIR / "agent_traj"

AGENT_CONDITIONS = ["ag_main"]   # ag_main_cc (Claude Code) is run by 1d-run_claude_code.py
IMAGE            = "dafny-agent-sandbox:2"   # :2 adds the resolve stub (2026-10-25)
SUBMIT           = "TASK_COMPLETE"
MAX_OUTPUT_CHARS = 10_000     # longer command output keeps its head and tail
SUSPICIOUS       = re.compile(r"\bassume\b|\{:\s*verify\s+false\s*\}|\{:\s*axiom\s*\}|\bexpect\b")
ACTION_REGEX     = r"```(?:bash|sh)\s*\n(.*?)```"
SUBMIT_COMMAND   = re.compile(r"echo\s+['\"]?" + SUBMIT + r"['\"]?")
# Providers for the OpenAI-compatible endpoints in 1-run_evaluation.MODELS, as LiteLLM names them
LITELLM_PROVIDER = {"https://api.together.xyz/v1": "together_ai", "https://openrouter.ai/api/v1": "openrouter"}
DOCKER_CANDIDATES = [Path.home() / ".docker" / "bin" / "docker",
                     Path("/Applications/Docker.app/Contents/Resources/bin/docker")]

# Observations rendered by mini-SWE-agent's model; same text as the earlier in-house loop.
OBSERVATION_TEMPLATE = (
    "{% if output.timed_out %}Command timed out after {{ output.timeout }} seconds."
    "{% else %}{{ output.output if output.output else '(no output)' }}"
    "{% if output.returncode not in [0, None] %}\n(exit code {{ output.returncode }}){% endif %}"
    "{% endif %}")
FORMAT_ERROR_TEMPLATE = prompts.AGENT_FORMAT_ERROR.replace("{n}", "{{ actions|length }}")


def find_docker() -> str | None:
    for c in [os.environ.get("DOCKER"), shutil.which("docker"), *map(str, DOCKER_CANDIDATES)]:
        if c and Path(c).exists():
            # the CLI calls its credential helpers by name, so their directory must be on PATH
            os.environ["PATH"] = f"{Path(c).resolve().parent}:{Path(c).parent}:{os.environ['PATH']}"
            return c
    return None


def litellm_settings(model: str) -> dict:
    """LiteLLMTextbasedModel arguments carrying exactly the settings in runner.MODELS."""
    cfg = runner.MODELS[model]
    kwargs = dict(cfg["extra_kwargs"])
    if cfg["client"] == "anthropic":
        # prompt caching: Anthropic needs explicit cache_control breakpoints (on the last
        # messages, so each turn reads the previous turn's prefix from cache)
        name, cache = f"anthropic/{cfg['model_id']}", "default_end"
    else:
        # DeepSeek and DeepInfra cache repeated prefixes automatically (no breakpoints needed);
        # cached_input_tokens in each conversation file shows whether it happened
        name, cache = f"{LITELLM_PROVIDER[cfg['base_url']]}/{cfg['model_id']}", None
        if cfg.get("extra_body"):
            kwargs["extra_body"] = cfg["extra_body"]
    # keys are read from the environment (never stored in model_kwargs, which are saved
    # in the trajectory). LiteLLM reads ANTHROPIC_API_KEY and OPENROUTER_API_KEY; ours are
    # copied over (overwriting any other key there) so every mode uses the same keys
    litellm_env = {"API_KEY_ANTHROPIC_ICST": "ANTHROPIC_API_KEY",
                   "API_KEY_OPEN_ROUTER_ICST": "OPENROUTER_API_KEY"}
    if cfg["api_key_env"] in litellm_env and os.environ.get(cfg["api_key_env"]):
        os.environ[litellm_env[cfg["api_key_env"]]] = os.environ[cfg["api_key_env"]]
    return {"model_name": name, "model_kwargs": kwargs, "set_cache_control": cache,
            "cost_tracking": "ignore_errors", "action_regex": ACTION_REGEX,
            "format_error_template": FORMAT_ERROR_TEMPLATE, "observation_template": OBSERVATION_TEMPLATE}


def truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n... ({len(text) - MAX_OUTPUT_CHARS} characters omitted) ...\n{text[-half:]}"


def read(path: Path):
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


# ─── conversation state ───────────────────────────────────────────────────────

def conv_path(condition: str, model: str, item: dict, run: int) -> Path:
    stem = Path(item["file"]).stem
    return RESP_DIR / condition / model / f"{item['tier']}_{item['task']}_{stem}_run{run}.json"


def new_state(condition: str, model: str, item: dict, run: int, limits: dict) -> dict:
    return {
        "item_id": item["item_id"], "tier": item["tier"], "task": item["task"],
        "mutation_type": item["mutation_type"], "base_program": item["base_program"],
        "file": item["file"], "model": model, "condition": condition, "run_index": run,
        "limits": limits, "status": "active", "started": datetime.now(timezone.utc).isoformat(),
        "harness": None, "messages": [],
        "steps": [], "verify_calls": [], "resolve_calls": [], "tokens": {"input": 0, "output": 0},
        "first_edit_step": None, "first_diagnosis_step": None,
    }


def save(state: dict):
    path = conv_path(state["condition"], state["model"], state, state["run_index"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


# ─── mini-SWE-agent classes ───────────────────────────────────────────────────

def harness_classes():
    """Import mini-SWE-agent lazily (only runs need it) and define the study subclasses."""
    os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
    import minisweagent
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.environments.docker import DockerEnvironment
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.models.litellm_textbased_model import LitellmTextbasedModel
    from minisweagent.exceptions import FormatError, InterruptAgentFlow, LimitsExceeded, Submitted
    import litellm
    litellm.suppress_debug_info = True
    logging.getLogger("minisweagent").setLevel(logging.WARNING)   # its console handler logs every command

    def exit_msg(status: str, content: str = ""):
        return {"role": "exit", "content": content, "extra": {"exit_status": status, "submission": ""}}

    class VerifyMixin:
        """`verify` on the host, timeouts and truncation; shared by both sandboxes."""
        def setup_study(self, state: dict, ws: Path, dafny_timeout: int):
            self.state, self.ws, self.dafny_timeout = state, ws, dafny_timeout

        def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict:
            if action.get("command", "").strip() == "verify":
                return {"output": self.verify(), "returncode": 0, "exception_info": "", "timed_out": False}
            if action.get("command", "").strip() == "resolve":
                return {"output": self.resolve(), "returncode": 0, "exception_info": "", "timed_out": False}
            out = self.run_command(action, cwd, timeout=timeout)
            out["timed_out"] = False
            if out.get("extra", {}).get("exception_type") == "TimeoutExpired":
                self.kill_all()
                out.update(timed_out=True, timeout=timeout or self.config.timeout, returncode=None)
            else:
                out["output"] = truncate(out.get("output") or "")
            self._check_finished(out)
            return out

        def _check_finished(self, output: dict):
            """mini-SWE-agent's submission rule with the study's token (echo TASK_COMPLETE)."""
            lines = (output.get("output") or "").lstrip().splitlines()
            if lines and lines[0].strip() == SUBMIT and output.get("returncode") == 0:
                raise Submitted(exit_msg("submitted"))

        def resolve(self) -> str:
            """`dafny resolve` on the host: parse and type check only, never verification,
            so it gives no way around the verify limit. Unlimited; every call is recorded."""
            program = read(self.ws / "program.dfy") or ""
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "program.dfy"
                path.write_text(program)
                try:
                    r = subprocess.run([str(iterative.DAFNY), "resolve", "--allow-warnings", "program.dfy"],
                                       capture_output=True, text=True, timeout=self.dafny_timeout + 10, cwd=d)
                    code, output = r.returncode, collector.sanitize_paths(r.stdout + r.stderr, str(path))
                except subprocess.TimeoutExpired:
                    code, output = None, ""
            self.state.setdefault("resolve_calls", []).append(
                {"step": len(self.state["steps"]) + 1, "exit_code": code, "ok": code == 0})
            if code == 0:
                return prompts.AGENT_RESOLVE_PASS
            if code is None:
                return prompts.AGENT_RESOLVE_TIMEOUT.format(timeout=self.dafny_timeout)
            return prompts.AGENT_RESOLVE_FAIL.format(output=truncate(output.strip()))

        def verify(self) -> str:
            state, ws = self.state, self.ws
            calls, limit = state["verify_calls"], state["limits"]["verify"]
            if len(calls) >= limit:
                return prompts.AGENT_VERIFY_LIMIT.format(verify_limit=limit)
            program = read(ws / "program.dfy") or ""
            output, code = iterative.run_dafny(program, self.dafny_timeout)
            calls.append({"step": len(state["steps"]) + 1, "program": program,
                          "diagnosis_json": read(ws / "diagnosis.json"),
                          "exit_code": code, "timeout": code is None, "pass": code == 0})
            remaining = limit - len(calls)
            if code == 0:
                raise Submitted(exit_msg("verified", prompts.AGENT_VERIFY_PASS))
            if code is None:
                text = prompts.AGENT_VERIFY_TIMEOUT.format(timeout=self.dafny_timeout, remaining=remaining)
            else:
                fields = collector.build_prompt_fields(program, output, "program.dfy")
                text = prompts.AGENT_VERIFY_FAIL.format(error_message=fields["error_message"],
                                                        counterexample_nl=fields["counterexample_nl"],
                                                        remaining=remaining)
            if remaining == 0:
                raise LimitsExceeded(exit_msg("verify_limit", text))
            return text

    class StudyDocker(VerifyMixin, DockerEnvironment):
        def run_command(self, action, cwd="", *, timeout=None):
            return DockerEnvironment.execute(self, action, cwd, timeout=timeout)

        def kill_all(self):
            # the command keeps running in the container; kill everything but PID 1
            subprocess.run([self.config.executable, "exec", self.container_id, "sh", "-c",
                            "kill -9 $(ps -eo pid= | grep -vw 1) 2>/dev/null"], capture_output=True)

        def cleanup(self):
            if getattr(self, "container_id", None):
                subprocess.run([self.config.executable, "rm", "-f", self.container_id], capture_output=True)
                self.container_id = None

    class StudyLocal(VerifyMixin, LocalEnvironment):
        """Not isolated (testing only). Minimal environment: no host variables or API keys."""
        def run_command(self, action, cwd="", *, timeout=None):
            try:
                r = subprocess.run(["bash", "-c", action.get("command", "")], cwd=self.config.cwd,
                                   env=self.config.env, text=True, timeout=timeout or self.config.timeout,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                return {"output": r.stdout, "returncode": r.returncode, "exception_info": ""}
            except subprocess.TimeoutExpired as e:
                return {"output": "", "returncode": -1, "exception_info": str(e),
                        "extra": {"exception_type": "TimeoutExpired", "exception": str(e)}}

        def kill_all(self):
            pass

        def cleanup(self):
            pass

    class StudyAgent(DefaultAgent):
        def setup_study(self, state: dict, ws: Path, item: dict):
            self.state, self.ws = state, ws
            self.last_prog, self.last_diag = item["source"], None

        def query(self) -> dict:
            if self.state["tokens"]["input"] + self.state["tokens"]["output"] > self.state["limits"]["tokens"]:
                raise LimitsExceeded(exit_msg("token_limit"))
            self.t_call = None
            if 0 < self.config.step_limit <= self.n_calls or (
                    0 < self.config.wall_time_limit_seconds <= int(time.time() - self._start_time)):
                return super().query()          # raises the limit exit
            self.t_call = time.time()
            message = super().query()
            # keep the history as plain answer text, without reasoning/thinking fields
            text = message.get("content") or ""
            reasoning = message.get("reasoning_content") or ""
            _, clean = scoring.get_think_and_clean({"raw_response": text, "reasoning_content": reasoning})
            self.messages[-1] = {"role": "assistant", "content": clean, "extra": message.get("extra", {})}
            return self.messages[-1] | {"_raw": text, "_reasoning": reasoning}

        def step(self) -> list[dict]:
            rec = {"step": len(self.state["steps"]) + 1, "raw_response": None, "reasoning_content": None,
                   "finish_reason": None, "usage": None, "elapsed_s": None, "command": None, "kind": None,
                   "output": None, "exit_code": None, "program_changed": False,
                   "diagnosis_changed": False, "program": None, "diagnosis_json": None}
            called = False
            try:
                message = self.query()
                called = True
                self.record_response(rec, message.get("extra", {}).get("response"),
                                     message["_raw"], message["_reasoning"])
                cmd = message["extra"]["actions"][0]["command"]
                rec["command"] = cmd
                rec["kind"] = ("verify" if cmd.strip() == "verify" else
                               "resolve" if cmd.strip() == "resolve" else
                               "submit" if SUBMIT_COMMAND.fullmatch(cmd) else "command")
                observations = self.execute_actions(message)
                rec["output"] = observations[0]["content"]
                rec["exit_code"] = observations[0].get("extra", {}).get("returncode")
                if rec["kind"] in ("verify", "resolve"):
                    rec["exit_code"] = None
                return observations
            except FormatError as e:
                called = True
                extra = e.messages[0].get("extra", {})
                self.record_response(rec, extra.get("response"), extra.get("model_response", ""), None)
                rec["kind"], rec["output"] = "format_error", e.messages[0]["content"]
                raise
            except InterruptAgentFlow as e:
                if called:                       # verify pass / verify limit / submission
                    rec["output"] = e.messages[0].get("content") or None
                    if rec["kind"] == "submit":
                        rec["output"] = None
                raise
            finally:
                if called:
                    self.finish_step(rec)

        def record_response(self, rec, response, raw, reasoning):
            response = response if isinstance(response, dict) else {}
            usage = response.get("usage") or {}
            choice = (response.get("choices") or [{}])[0]
            if reasoning is None:
                msg = choice.get("message") or {}
                reasoning = msg.get("reasoning_content") or ""
            rec.update(raw_response=raw, reasoning_content=reasoning,
                       finish_reason=choice.get("finish_reason"),
                       usage={**{k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                                                            "cache_read_input_tokens", "cache_creation_input_tokens")},
                              "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                              "provider": response.get("provider")},
                       elapsed_s=round(time.time() - self.t_call, 2) if self.t_call else None)
            self.state["tokens"]["input"] += usage.get("prompt_tokens") or 0
            self.state["tokens"]["output"] += usage.get("completion_tokens") or 0
            # cache hits are part of input; tracked separately to check that caching works
            self.state.setdefault("cached_input_tokens", 0)
            self.state["cached_input_tokens"] += (usage.get("cache_read_input_tokens")
                                                  or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)

        def finish_step(self, rec):
            # record changes to the two files that are scored
            prog, diag = read(self.ws / "program.dfy"), read(self.ws / "diagnosis.json")
            if prog != self.last_prog:
                rec["program_changed"], rec["program"], self.last_prog = True, prog, prog
                if self.state["first_edit_step"] is None:
                    self.state["first_edit_step"] = rec["step"]
            if diag != self.last_diag:
                rec["diagnosis_changed"], rec["diagnosis_json"], self.last_diag = True, diag, diag
                if self.state["first_diagnosis_step"] is None and diag is not None:
                    self.state["first_diagnosis_step"] = rec["step"]
            self.state["steps"].append(rec)
            self.state["messages"] = [{"role": m["role"], "content": m["content"]}
                                      for m in self.messages if m.get("role") in ("system", "user", "assistant")]
            save(self.state)

    return {"version": minisweagent.__version__, "agent": StudyAgent, "docker": StudyDocker,
            "local": StudyLocal, "model": LitellmTextbasedModel}


# ─── one conversation ─────────────────────────────────────────────────────────

EXIT_STATUS = {"Submitted": "submitted", "LimitsExceeded": "step_limit", "TimeExceeded": "time_limit"}


def run_conversation(state: dict, item: dict, sandbox_kind: str, args, logger, H: dict) -> dict:
    lim = state["limits"]
    ws = Path(tempfile.mkdtemp(prefix="ws_"))
    (ws / "program.dfy").write_text(item["source"])
    if sandbox_kind == "docker":
        env = H["docker"](image=args.image, cwd="/work", executable=args.docker, timeout=args.command_timeout,
                          interpreter=["bash", "-c"], container_timeout=f"{lim['seconds'] + 600}s",
                          run_args=["--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
                                    "--memory", "2g", "--cpus", "1", "--pids-limit", "256",
                                    "-e", "HOME=/work", "-v", f"{ws}:/work"])
    else:
        env = H["local"](cwd=str(ws), timeout=args.command_timeout,
                         env={"PATH": f"{EVAL_DIR / 'agent_sandbox'}:/usr/bin:/bin:/usr/sbin:/sbin",
                              "HOME": str(ws), "LANG": "C.UTF-8", "TERM": "dumb"})
    env.setup_study(state, ws, args.timeout)
    model = H["model"](**litellm_settings(state["model"]))
    traj = TRAJ_DIR / state["condition"] / state["model"] / conv_path(state["condition"], state["model"],
                                                                      item, state["run_index"]).name
    agent = H["agent"](model, env, system_template="{{ protocol }}", instance_template="{{ task }}",
                       step_limit=lim["steps"], cost_limit=0, wall_time_limit_seconds=lim["seconds"],
                       max_consecutive_format_errors=0, output_path=traj)
    agent.setup_study(state, ws, item)
    state["harness"] = {"name": "mini-swe-agent", "version": H["version"], "sandbox": sandbox_kind,
                        "image": args.image if sandbox_kind == "docker" else None}
    t_start = time.time()
    try:
        try:
            result = agent.run(task=prompts.build_agent_task(item, state["condition"]),
                               protocol=prompts.AGENT_PROTOCOL.format(verify_limit=lim["verify"],
                                                                      step_limit=lim["steps"]))
            status = result.get("exit_status", "")
            state["status"] = EXIT_STATUS.get(status, status)
        except Exception as e:                  # the API failed after mini-SWE-agent's retries
            logger.warning(f"{state['item_id']}: {type(e).__name__}: {e}")
            state["status"], state["error"] = "error", f"{type(e).__name__}: {str(e)[:500]}"
        state["messages"] = [{"role": m["role"], "content": m["content"]}
                             for m in agent.messages if m.get("role") in ("system", "user", "assistant")]
        # final state and an independent check of the final program
        last_prog = read(ws / "program.dfy")
        state["final_program"] = last_prog
        state["final_diagnosis_json"] = read(ws / "diagnosis.json")
        prev = state["verify_calls"][-1] if state["verify_calls"] else None
        if prev and prev["program"] == last_prog and prev["exit_code"] is not None:
            state["final_pass"] = prev["pass"]
        else:
            _, code = iterative.run_dafny(last_prog or "", args.timeout)
            state["final_pass"] = code == 0
        state["wall_s"] = round(time.time() - t_start, 1)
        state["finished"] = datetime.now(timezone.utc).isoformat()
        save(state)
        logger.info(f"{state['item_id']} run{state['run_index']}: {state['status']} "
                    f"steps={len(state['steps'])} verify={len(state['verify_calls'])} pass={state['final_pass']}")
    finally:
        env.cleanup()
        shutil.rmtree(ws, ignore_errors=True)
    return state


# ─── scoring ──────────────────────────────────────────────────────────────────

def parse_diagnosis(text):
    """Score a diagnosis.json snapshot like a single-shot answer."""
    d = scoring.extract_json(text) if text else None
    if not isinstance(d, dict):
        return {"ok": False, "spec": "", "obl": "", "text": ""}
    return {"ok": True, "spec": scoring.normalize_spec_element(str(d.get("spec_element", ""))),
            "obl": scoring.normalize_obligation(str(d.get("proof_obligation", ""))),
            "text": str(d.get("diagnosis", ""))}


def mutation_site(item: dict, original: str):
    """(k, j1, j2): the clause's first line in the original and the mutated lines' span in the mutant."""
    kw = equiv.TASK_TO_KEYWORD[item["task"]]
    k, mline = equiv.locate_original_clause(original, item["source"], kw)
    if k is None:
        return None, None, None
    o, m = original.splitlines(), item["source"].splitlines()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, o, m, autojunk=False).get_opcodes():
        if tag != "equal" and i1 <= k < max(i2, i1 + 1):
            # continuation lines of the clause are unchanged by the mutation; include them
            end_orig = equiv.clause_extent(o, k)
            return k, j1, j2 + (end_orig - i2 if end_orig > i2 else 0)
    return k, None, None


def norm(line: str) -> str:
    text = re.sub(r"\s+", " ", equiv.strip_line_comment(line)).strip()
    # a trailing ";" on a spec clause is deprecated syntax with no meaning; removing it (as
    # agents do to silence Dafny's warning) is not an edit (amended 2026-10-26)
    return text[:-1].rstrip() if text.endswith(";") and equiv.CLAUSE_RE.match(line) else text


EDIT_FLAGS = ["site", "proof_added", "proof_removed", "contract", "signature", "code", "escape"]
PROOF_STMT = re.compile(r"^\s*(assert|calc|reveal)\b")


def nows(line: str) -> str:
    """norm without any whitespace: `r:=x;` and `r := x;` are the same line (amended
    2026-10-27, reformatting was flagged as a code or signature edit)."""
    return re.sub(r"\s+", "", norm(line))


def in_header(lines: list[str]) -> list[bool]:
    """True for the lines of a declaration's header (from the declaration line up to the
    line that opens its body), where requires/ensures are the declaration's contract. An
    ensures inside a body belongs to a forall statement, i.e. a proof (amended 2026-10-27)."""
    out, head = [], False
    for line in lines:
        t = norm(line)
        if equiv.DECL_RE.match(line):
            head = True
            out.append(True)
            if t.endswith("{") and not equiv.CLAUSE_RE.search(t):
                head = False
            continue
        if head and t.startswith("{"):
            head = False
        out.append(head)
    return out


def _block_end(lines: list[str], start: int) -> int | None:
    """Index of the line closing the block whose `{` is on or after line `start`."""
    depth, opened = 0, False
    for i in range(start, len(lines)):
        t = norm(lines[i])
        for ch in t:
            if ch == "{":
                depth, opened = depth + 1, True
            elif ch == "}":
                depth -= 1
        if opened and depth <= 0:
            return i
    return None


GHOST_BLOCK = re.compile(r"^\}?\s*(else\b|if\b|forall\b|assert\b.*\bby\s*\{)")


def decl_names(lines: list[str]) -> list[str | None]:
    """For each line, the name of the declaration (method, function, lemma...) it belongs to."""
    out, cur = [], None
    for line in lines:
        m = equiv.DECL_RE.match(line)
        if m:
            n = re.match(r"\s*(?:\{:[^}]*\}\s*)*(\w+)", line[m.end():])
            cur = n.group(1) if n else None
        out.append(cur)
    return out


def line_kinds(lines: list[str]) -> list[tuple[str | None, str | None]]:
    """What each line of a program is, for classifying edits outside the mutation site:
      proof      loop invariants, decreases, assert/calc/reveal, ghost variables, lemma calls,
                 anything inside a lemma
      contract   requires/ensures/modifies/reads of a method or function, and the bodies of
                 functions and predicates (specifications call them)
      signature  a method/function/predicate declaration line (name, parameters, types)
      code       anything else: executable statements of methods
      None       no content (blank, comment, braces)
    A line continuing a clause or proof statement (the previous one didn't end with ; { or })
    gets that statement's kind. Each entry is (kind, clause keyword): the keyword of the
    requires/ensures/invariant/... clause the line belongs to, else None."""
    lemmas = {m.group(1) for l in lines
              for m in [re.match(r"^\s*(?:\S+\s+)*?lemma\s+(?:\{:[^}]*\}\s*)*(\w+)", l)] if m}
    header = in_header(lines)
    out, decl, pending, pending_kw = [], None, None, None
    for n_line, line in enumerate(lines):
        t = norm(line)
        m = equiv.DECL_RE.match(line)
        if m:
            decl = m.group(1)
        if not t or re.fullmatch(r"[{}();,\s]*", t):
            kind = None
        elif decl == "lemma":
            kind = "proof"
        elif m:
            kind = "signature"
        elif equiv.CLAUSE_RE.match(line):
            ckw = equiv.CLAUSE_RE.match(line).group(1)
            kind = ("proof" if ckw in ("invariant", "decreases")
                    or (ckw in ("requires", "ensures") and decl in ("method", "constructor")
                        and not header[n_line]) else "contract")
        elif PROOF_STMT.match(line) or re.match(r"^\s*ghost\b", line):
            kind = "proof"
        elif (c := re.match(r"^\s*(?:\w+\.)*(\w+)\s*\(", line)) and c.group(1) in lemmas:
            kind = "proof"
        elif pending:
            kind = pending
        elif decl in ("function", "predicate"):
            kind = "contract"
        else:
            kind = "code"
        c = equiv.CLAUSE_RE.match(line)
        kw = c.group(1) if c else (pending_kw if kind is not None and kind == pending else None)
        if t:
            pending = kind if kind in ("proof", "contract") and not re.search(r"[;{}]$", t) else None
            pending_kw = kw if pending else None
        out.append((kind, kw))
    # if/else/forall/assert-by blocks that contain only proof are ghost code: the block's
    # control lines are proof too; a forall statement with an ensures and an assert-by block
    # are proof throughout (amended 2026-10-27)
    changed = True
    while changed:
        changed = False
        for i, (kind, kw) in enumerate(out):
            t = norm(lines[i])
            assert_by = re.match(r"^\}?\s*assert\b.*\bby\s*\{", t)
            if not GHOST_BLOCK.match(t) or (kind != "code" and not assert_by):
                continue
            end = _block_end(lines, i)
            if end is None:
                continue
            body = range(i + 1, end)
            whole = assert_by or (re.match(r"^forall\b", t) and (re.search(r"\bensures\b", t) or any(
                re.match(r"^\s*ensures\b", lines[j]) for j in body)))
            if whole or all(out[j][0] in (None, "proof") for j in body):
                for j in [i, *body]:
                    if out[j][0] not in (None, "proof"):
                        out[j] = ("proof", out[j][1])
                        changed = True
    return out


def edit_analysis(item: dict, original: str, final: str):
    """
    Compare the final program with the mutant. Returns (edit_scope, site_text, details):
      none           program.dfy unchanged
      site_only      only the mutated clause changed
      site_and_proof the mutated clause plus added/changed assert lines
      other_spec     another specification clause changed
      implementation code outside specification clauses changed
    site_text is the final text at the mutation site (None if the site wasn't found).
    Whitespace-only and comment-only differences are ignored.
    details["flags"] says what changed, one boolean per EDIT_FLAGS entry (several can be
    true): site = the mutated clause; outside it, line_kinds' proof (added / removed lines),
    contract, signature and code; escape = assume, {:verify false}, {:axiom} or expect added.
    """
    k, j1, j2 = mutation_site(item, original)
    mut, fin = item["source"].splitlines(), (final or "").splitlines()
    sm = difflib.SequenceMatcher(None, [nows(l) for l in mut], [nows(l) for l in fin], autojunk=False)
    fhead = in_header(fin)
    site_parts, outside, kinds = [], [], set()
    flags = dict.fromkeys(EDIT_FLAGS, False)
    mkind, fkind = line_kinds(mut), line_kinds(fin)
    site_kw = equiv.TASK_TO_KEYWORD[item["task"]]
    mname, fname = decl_names(mut), decl_names(fin)
    # the declaration holding the mutated clause (for a deleted clause, the line before the gap)
    site_decl = mname[max(j1 - 1, 0) if j1 == j2 else j1] if j1 is not None and mut else None
    relocated = []      # clauses of the mutated kind added to the same declaration, elsewhere
    for tag, i1, i2, f1, f2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if "".join(map(nows, mut[i1:i2])) == "".join(map(nows, fin[f1:f2])):
            continue                         # line breaks moved, nothing else (amended 2026-10-27)
        if j1 is None:
            touches = False
        elif j1 == j2:                       # clause deleted by the mutation: an insertion point
            touches = i1 <= j1 <= i2
        else:
            touches = (i1 < j2 and i2 > j1) if i2 > i1 else j1 <= i1 <= j2
        if touches:
            site_parts.append((i1, i2, f1, f2))
            # lines in the site hunk that aren't clauses of the mutated kind (e.g. a changed
            # method signature right above a requires) still count as edits of their kind
            for side, kinds_, lo, hi in (("removed", mkind, i1, i2), ("added", fkind, f1, f2)):
                for kind, kw in kinds_[lo:hi]:
                    if kind is None or kw == site_kw:
                        continue
                    flags[f"proof_{side}" if kind == "proof" else kind] = True
        else:
            outside.append((i1, i2, f1, f2))
            for side, kinds_, lo, hi in (("removed", mkind, i1, i2), ("added", fkind, f1, f2)):
                for n, (kind, kw) in enumerate(kinds_[lo:hi]):
                    if (side == "added" and kw == site_kw and site_decl and fname[lo + n] == site_decl
                            and (site_kw == "invariant" or fhead[lo + n])):
                        # the repair, placed a few lines away from the mutation site (clause
                        # order doesn't matter); it is part of the site text below
                        relocated.append(fin[lo + n])
                        flags["site"] = True
                    elif kind == "proof":
                        flags[f"proof_{side}"] = True
                    elif kind:
                        flags[kind] = True
            for line in mut[i1:i2] + fin[f1:f2]:
                if not norm(line):
                    continue
                if equiv.CLAUSE_RE.match(line):
                    kinds.add("other_spec")
                elif re.match(r"^\s*assert\b", line):
                    kinds.add("proof")
                else:
                    kinds.add("implementation")
    added = [l for _, _, f1, f2 in site_parts + outside for l in fin[f1:f2]]
    details = {"lines_changed_outside": sum(max(i2 - i1, f2 - f1) for i1, i2, f1, f2 in outside),
               "suspicious": sorted({m.group(0) for l in added for m in [SUSPICIOUS.search(l)] if m})}
    flags["site"] = flags["site"] or bool(site_parts)
    flags["escape"] = bool(details["suspicious"])
    details["flags"] = flags
    if not site_parts and not outside:
        return "none", None, details
    if "implementation" in kinds:
        scope = "implementation"
    elif "other_spec" in kinds:
        scope = "other_spec"
    elif "proof" in kinds:
        scope = "site_and_proof" if site_parts else "proof_only"
    else:
        scope = "site_only" if site_parts else "none"
    if j1 is None:
        return scope, None, details
    # final text at the site: the mutant's site lines with the touching hunks applied
    lo = min([j1] + [i1 for i1, _, _, _ in site_parts])
    hi = max([j2] + [i2 for _, i2, _, _ in site_parts])
    text = []
    for tag, i1, i2, f1, f2 in sm.get_opcodes():
        if tag == "equal" or ((i1, i2, f1, f2) not in site_parts and i2 - i1 == f2 - f1
                              and "".join(map(nows, mut[i1:i2])) == "".join(map(nows, fin[f1:f2]))):
            a, b = max(i1, lo), min(i2, hi)
            if a < b:
                text += fin[f1 + a - i1: f1 + b - i1]
        elif (i1, i2, f1, f2) in site_parts:
            text += fin[f1:f2]
    return scope, "\n".join(text + relocated), details


def site_clauses(site_text: str, keyword: str):
    """The clauses at the site, for equiv.classify_repair: (list of clauses, reason if not
    possible). `decreases` clauses are proof annotations and dropped (amended 2026-10-27)."""
    clauses, cur = [], []
    for line in (site_text or "").splitlines():
        if not norm(line):
            continue
        m = equiv.CLAUSE_RE.match(line)
        if m:
            if cur:
                clauses.append(cur)
            cur = [m.group(1), line[m.end():]]
        elif cur:
            cur[1] += "\n" + line
        else:
            return None, "site contains a non-clause line"
    if cur:
        clauses.append(cur)
    clauses = [c for c in clauses if c[0] != "decreases"]
    if not clauses:
        return [f"{keyword} true"], None        # clause deleted: weakest possible
    if any(c[0] != keyword for c in clauses):
        return None, "repair is a different kind of clause"
    return [f"{keyword} {equiv.clean_clause(c[1])}" for c in clauses], None


def site_clause(site_text: str, keyword: str):
    """The site as one clause (the conjunction), for reporting: (clause, reason)."""
    clauses, reason = site_clauses(site_text, keyword)
    if clauses is None:
        return None, reason
    if len(clauses) == 1:
        return clauses[0], None
    return f"{keyword} " + " && ".join(f"({equiv.clean_clause(c, keyword)})" for c in clauses), None


def fidelity_job(args):
    item, original, site_text, timeout = args
    kw = equiv.TASK_TO_KEYWORD[item["task"]]
    k, _ = equiv.locate_original_clause(original, item["source"], kw)
    if k is None:
        return "undetermined", None, None, "original clause not located"
    clauses, reason = site_clauses(site_text, kw)
    if clauses is None:
        return "undetermined", None, None, reason
    return equiv.classify_repair(original, k, kw, clauses, timeout)


def score(condition: str, workers: int, timeout: int):
    import pandas as pd
    items = {i["item_id"]: i for i in json.loads(BENCHMARK.read_text())["items"]}
    rows, fid_jobs = [], []
    for path in sorted((RESP_DIR / condition).glob("*/*.json")):
        s = json.loads(path.read_text())
        item = items.get(s["item_id"])
        if item is None or s["status"] == "active":
            continue
        original = (CORPUS / item["tier"] / item["task"] / f"{item['base_program']}.dfy").read_text()
        gt_spec = scoring.TASK_TO_SPEC[s["task"]]
        gt_obl = scoring.obligation_set(item.get("gt_proof_obligation", "unknown"))
        diags = [st["diagnosis_json"] for st in s["steps"] if st["diagnosis_changed"] and st["diagnosis_json"]]
        first, last = parse_diagnosis(diags[0] if diags else None), parse_diagnosis(s.get("final_diagnosis_json"))
        scope, site_text, det = edit_analysis(item, original, s.get("final_program"))
        kw = equiv.TASK_TO_KEYWORD[s["task"]]
        clause, _ = site_clause(site_text, kw) if site_text is not None else (None, None)
        fe, fd = s.get("first_edit_step"), s.get("first_diagnosis_step")
        row = {"model": s["model"], "item_id": s["item_id"], "run": s["run_index"], "tier": s["tier"],
               "task": s["task"], "mutation_type": s["mutation_type"], "status": s["status"],
               "steps": len(s["steps"]), "verify_calls": len(s["verify_calls"]),
               "resolve_calls": len(s.get("resolve_calls", [])),
               "format_errors": sum(st["kind"] == "format_error" for st in s["steps"]),
               "input_tokens": s["tokens"]["input"], "output_tokens": s["tokens"]["output"],
               "cached_input_tokens": s.get("cached_input_tokens", 0),
               "wall_s": s.get("wall_s"), "dafny_pass": bool(s.get("final_pass")),
               "edit_scope": scope, "lines_changed_outside": det["lines_changed_outside"],
               "suspicious_edits": "|".join(det["suspicious"]),
               **{f"edit_{k}": v for k, v in det["flags"].items()},
               "edit_flags": "|".join(k for k, v in det["flags"].items() if v) or "none",
               # a verifying repair made where the fault is, without changing other contracts,
               # signatures or code or adding escape hatches (proof edits allowed); the agentic
               # counterpart of the single-clause repairs of the other modes (amended 2026-10-26)
               "clean_pass": bool(s.get("final_pass")) and det["flags"]["site"] and not any(
                   det["flags"][k] for k in ("contract", "signature", "code", "escape")),
               "pred_repair": clause,
               "diagnosis_written": fd is not None,
               "diagnosis_before_edit": fd is not None and (fe is None or fd <= fe),
               "gt_proof_obligation": item.get("gt_proof_obligation", "unknown")}
        for tag, d in (("first", first), ("last", last)):
            row[f"{tag}_diag_ok"] = d["ok"]
            row[f"{tag}_spec_element"] = d["spec"]
            row[f"{tag}_spec_correct"] = d["spec"] == gt_spec
            row[f"{tag}_proof_obligation"] = d["obl"]
            row[f"{tag}_obligation_correct"] = (d["obl"] in gt_obl) if gt_obl else None
            row[f"{tag}_diagnosis"] = d["text"]
        rows.append(row)
        if row["dafny_pass"] and site_text is not None:
            fid_jobs.append((row, (item, original, site_text, timeout)))
    if not rows:
        print(f"No finished conversations in {RESP_DIR / condition}; nothing to score.")
        return
    out = SCORES / condition
    out.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out / "agent_runs.csv", index=False)
    df[["model", "item_id", "run", "tier", "task", "mutation_type", "pred_repair", "dafny_pass",
        "status", "steps", "edit_scope"]].to_csv(out / "repair_scores.csv", index=False)
    fids = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for row, res in zip([r for r, _ in fid_jobs], ex.map(fidelity_job, [j for _, j in fid_jobs])):
            fid, fwd, bwd, reason = res
            fids.append({k: row[k] for k in ("model", "item_id", "run", "tier", "task", "mutation_type",
                                              "pred_repair", "edit_scope")} |
                        {"fidelity": fid, "orig_implies_repair": fwd, "repair_implies_orig": bwd,
                         "undetermined_reason": reason})
    pd.DataFrame(fids).to_csv(out / "fidelity_scores.csv", index=False)
    print(f"{len(df)} conversations, {len(fids)} passing with a located site → {out}/")
    if len(df):
        print("\nStatus by model:\n" + df.groupby(["model", "status"]).size().unstack(fill_value=0).to_string())
        print("\nPass rate:\n" + df.groupby("model")["dafny_pass"].mean().round(3).to_string())
        print("\nEdit scope (passing):\n" + df[df.dafny_pass].groupby(["model", "edit_scope"]).size()
              .unstack(fill_value=0).to_string())
        print("\nWhat passing runs changed (share):\n" + df[df.dafny_pass].groupby("model")[
            [f"edit_{k}" for k in EDIT_FLAGS]].mean().round(3).to_string())
        print("\nPass rate, any vs clean:\n" + df.groupby("model")[["dafny_pass", "clean_pass"]].mean()
              .round(3).to_string())
        print("\ndiagnosis.json before first edit:\n" + df.groupby("model")["diagnosis_before_edit"].mean().round(3).to_string())
    if fids:
        print("\nFidelity (passing):\n" + pd.DataFrame(fids).groupby(["model", "fidelity"]).size()
              .unstack(fill_value=0).to_string())


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", default="ag_main", choices=AGENT_CONDITIONS)
    ap.add_argument("--models", nargs="+", default=["claude-opus-5-5", "deepseek-v4-pro-0813", "qwen3.8-27b"],
                    choices=list(runner.MODELS))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--sandbox", choices=["docker", "local"], default="docker")
    ap.add_argument("--image", default=IMAGE)
    ap.add_argument("--docker", default=None, help="docker CLI path (default: $DOCKER, PATH, ~/.docker/bin, Docker.app)")
    ap.add_argument("--workers", type=int, default=8, help="Parallel conversations (sandboxes) per model")
    # 20 turns cover reading the program, diagnosis.json, five edit/verify cycles of about
    # three turns each, and submitting
    ap.add_argument("--step-limit", type=int, default=20, help="Model turns per conversation")
    ap.add_argument("--verify-limit", type=int, default=5)
    ap.add_argument("--time-limit", type=int, default=1800, help="Wall seconds per conversation")
    ap.add_argument("--token-limit", type=int, default=1_000_000, help="Input+output tokens per conversation")
    ap.add_argument("--timeout", type=int, default=20, help="Dafny time limit, as in 3-apply_repairs.py")
    ap.add_argument("--command-timeout", type=int, default=60)
    ap.add_argument("--subset", type=int, choices=runner.MODE_SUBSET_SIZES,
                    help="Run on the first N items of ii-corpus/mode_subset.json (N = 200 in the paper; required for main runs)")
    ap.add_argument("--limit", type=int, help="Stratified sample of N items (same seed as 1-run_evaluation.py); pilots")
    ap.add_argument("--items", nargs="+", help="Run only these item_ids")
    ap.add_argument("--tier")
    ap.add_argument("--task")
    ap.add_argument("--score", action="store_true", help="Score saved conversations; no API calls")
    args = ap.parse_args()

    if args.score:
        score(args.condition, args.workers, 15)
        return
    try:
        H = harness_classes()
    except ImportError:
        sys.exit("mini-swe-agent is not installed for this python; run with iii-evaluation/.venv-agent/bin/python "
                 "(see agent_sandbox/requirements.txt)")
    print(f"Harness: mini-swe-agent {H['version']}")
    if args.sandbox == "docker":
        if args.docker:
            os.environ["DOCKER"] = args.docker
        args.docker = find_docker()
        if args.docker is None:
            sys.exit("docker not found; install Docker Desktop, pass --docker PATH, or use --sandbox local for testing only")
        if subprocess.run([args.docker, "image", "inspect", args.image], capture_output=True).returncode:
            sys.exit(f"Image {args.image} not found: docker build -t {args.image} iii-evaluation/agent_sandbox")
    else:
        print("WARNING: --sandbox local is not isolated; use it only to test the harness.")

    if not (args.subset or args.limit or args.items):
        sys.exit("Pass --subset N (main run; N = 200 in the paper), or --limit N / --items (pilot, testing).")
    items = json.loads(BENCHMARK.read_text())["items"]
    if args.subset:
        items = runner.mode_subset(items, args.subset)
    if args.items:
        items = [i for i in items if i["item_id"] in set(args.items)]
    if args.tier:
        items = [i for i in items if i["tier"] == args.tier]
    if args.task:
        items = [i for i in items if i["task"] == args.task]
    if args.limit:
        items = runner.stratified_sample(items, args.limit)
    limits = {"steps": args.step_limit, "verify": args.verify_limit,
              "seconds": args.time_limit, "tokens": args.token_limit}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for model in args.models:
        cfg = runner.MODELS[model]
        if not os.environ.get(cfg["api_key_env"]):
            print(f"[{model}] WARNING: {cfg['api_key_env']} not set — skipping model.")
            continue
        logging.basicConfig(filename=str(LOG_DIR / f"{model}_{args.condition}.log"), level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s", force=True)
        logger = logging.getLogger(model)
        todo = []
        for item in items:
            for r in range(1, args.runs + 1):
                p = conv_path(args.condition, model, item, r)
                if p.exists() and json.loads(p.read_text())["status"] != "active":
                    continue
                todo.append((new_state(args.condition, model, item, r, limits), item))
        print(f"\n[{model}] {len(todo)} conversations to run ({len(items) * args.runs - len(todo)} done) "
              f"→ responses/{args.condition}/{model}/")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(run_conversation, s, it, args.sandbox, args, logger, H) for s, it in todo]
            for n, fut in enumerate(as_completed(futs), 1):
                try:
                    s = fut.result()
                    print(f"  [{n}/{len(todo)}] {s['item_id']} run{s['run_index']}: {s['status']}, "
                          f"{len(s['steps'])} steps, {len(s['verify_calls'])} verify, pass={s['final_pass']}")
                except Exception as e:
                    logger.exception("conversation failed")
                    print(f"  [{n}/{len(todo)}] FAILED: {e}")


if __name__ == "__main__":
    main()
