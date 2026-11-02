"""
prompts.py  —  Every prompt used in the ICST'27 study, by condition.

Condition names follow <mode>_<variation>. Each variation changes exactly one thing
relative to its mode's main condition, so differences in results can be attributed
to that change.

Every prompt starts with the Dafny reference (agent_context/DAFNY.md, REFERENCE_SECTION),
the same text in every condition and mode, so providers can cache it as a shared prefix.
The only exception is ss_nodafnyref, which measures what the reference contributes.

  Single-shot (one model call per item)
    ss_main        Dafny reference, then the program, Dafny error block and counterexample.
                   The main condition. The text after the reference is item["prompt"]
                   from benchmark.json, built by
                   i-corpus-construction/2-collect_verifier_output.py.
    ss_masked      ss_main with the error messages' wording hidden: each error keeps
                   its location (program.dfy(L,C)) and the counterexample, but
                   "could not be proved on entry" etc. becomes "verification failed".
                   Tests how much diagnosis depends on the verifier naming the
                   obligation (RQ1).
    ss_nodafnyref  ss_main without the Dafny reference (RQ4).
    ss_intent      ss_main with a natural-language description of the intended
                   behavior before the program (RQ4). Needs
                   ii-corpus/intent_descriptions.json.

  Iterative (up to k attempts with verifier feedback in between)
    it_main        Turn 1 is ss_main. After each failed attempt the model gets
                   ITERATIVE_FEEDBACK (or ITERATIVE_UNAPPLIED if its answer could not
                   be applied). Each repair is applied to the original mutant, and
                   ITERATIVE_FEEDBACK shows that patched program so the error
                   locations match what the model sees.

  Agentic (the model drives a sandbox; harness mini-SWE-agent unless noted)
    ag_main        Dafny reference, then AGENT_TASK with program.dfy, the error output
                   and counterexample. The agent runs Dafny only through two commands:
                   `verify` (at most 5 calls, as it_main's 5 attempts) and `resolve`
                   (syntax and type check, no verification, unlimited; added 2026-10-25
                   before any agent run). (ag_dafnyref, the reference as a file in the
                   sandbox, was dropped on 2026-10-25 when the reference moved into
                   every main condition.)
    ag_intent      ag_main plus the intended-behavior description. Dropped for budget
                   (2026-10-24); the intent effect is measured in single-shot only.
    ag_main_cc     Claude Code (Opus only), fully agentic: Dafny 4.11 installed in the
                   sandbox and run as often as the agent likes, all Claude Code tools
                   except web access. Same task as ag_main except steps 3-4
                   (AGENT_TASK_CC); Claude Code's own system prompt plus
                   AGENT_CC_APPEND. Run by 1d-run_claude_code.py (added 2026-10-26).

All prompts are neutral about the fault: they say the program fails verification,
never that it "has a specification mutation" (that would hint at the fault).
They still ask for a corrected specification line, matching the scenario where the
implementation is trusted and the specification is under review.

The answer format keeps the word "bridge" for the third proof obligation, as in the
earlier prompt versions; the study calls it "termination" (scoring maps one to the other).
"""

from __future__ import annotations
import json, re
from pathlib import Path

ROOT          = Path(__file__).parent.parent
DAFNY_REF     = Path(__file__).parent / "agent_context" / "DAFNY.md"
INTENT_FILE   = ROOT / "ii-corpus" / "intent_descriptions.json"

CONDITIONS = {
    "ss_main":       {"mode": "single_shot", "summary": "program + error + counterexample (main)"},
    "ss_masked":     {"mode": "single_shot", "summary": "error wording hidden, location and counterexample kept"},
    "ss_nodafnyref": {"mode": "single_shot", "summary": "main without the Dafny reference"},
    "ss_intent":     {"mode": "single_shot", "summary": "main + intended-behavior description"},
    "it_main":       {"mode": "iterative",   "summary": "ss_main, then verifier feedback for up to k attempts"},
    "ag_main":       {"mode": "agentic",     "summary": "agent in a sandbox (mini-SWE-agent)"},
    "ag_main_cc":    {"mode": "agentic",     "summary": "Claude Code, unlimited Dafny, no web (Opus only)"},
}
SINGLE_SHOT = [c for c, v in CONDITIONS.items() if v["mode"] == "single_shot"]

PROGRAM_HEADER    = "=== PROGRAM ==="

DEFINITIONS = """\
Definitions:
- requires: the precondition that must hold before the method executes
- invariant: the loop invariant that must hold throughout the loop
- ensures: the postcondition that the method must guarantee on return
- initiation: the precondition does not establish the invariant at loop entry
- preservation: the loop body does not maintain the invariant
- bridge: the invariant and loop exit condition do not imply the postcondition"""

ITERATIVE_FEEDBACK = """\
Your repair was applied to the program, and Dafny verification still fails. \
This is the program with your repair applied; the error locations refer to it.

=== PROGRAM ===
{source}

=== DAFNY ERROR ===
{error_message}

=== COUNTEREXAMPLE ===
{counterexample_nl}

Revise your diagnosis and repair. Respond with ONLY a valid JSON object in the same \
format as before.\
"""

ITERATIVE_UNAPPLIED = """\
Your previous response could not be applied to the program: {reason}.

Respond with ONLY a valid JSON object in the same format as before, with "repair" \
set to a single Dafny specification line.\
"""

UNAPPLIED_REASONS = {
    "no_json":          "it was not a valid JSON object",
    "no_repair":        "it did not contain a repair",
    "multiline_repair": "the repair spans several lines",
}

AGENT_TASK = """\
You are a formal verification assistant working in a terminal. The Dafny program in \
program.dfy fails Dafny verification.

Dafny's error output:
{error_message}

Counterexample:
{counterexample_nl}

Your task:
1. Diagnose the failure. Before your first edit to program.dfy, write diagnosis.json \
with exactly this structure:
   {{"spec_element": "<one of: requires | invariant | ensures>",
     "proof_obligation": "<one of: initiation | preservation | bridge>",
     "diagnosis": "<explanation of what broke and why>"}}
2. Repair the specification in program.dfy so that it verifies.
3. Check your work with the command `verify`, which runs Dafny on program.dfy. Update \
diagnosis.json before each call to `verify` if your understanding has changed.
4. Stop when `verify` reports success or when you cannot make further progress.

""" + DEFINITIONS

# ag_main_cc (1d-run_claude_code.py): the same task, but Dafny is installed in the sandbox
# and has no call limit, so steps 3-4 name Dafny instead of `verify`. Everything else is
# AGENT_TASK verbatim (the asserts keep the two in sync).
_AG_STEPS_34 = """\
3. Check your work with the command `verify`, which runs Dafny on program.dfy. Update \
diagnosis.json before each call to `verify` if your understanding has changed.
4. Stop when `verify` reports success or when you cannot make further progress.
"""
_CC_STEPS_34 = """\
3. Check your work by running Dafny on program.dfy (for example `dafny verify program.dfy`). \
Update diagnosis.json before each verification run if your understanding has changed.
4. Stop when Dafny verifies program.dfy or when you cannot make further progress.
"""
assert AGENT_TASK.count(_AG_STEPS_34) == 1
AGENT_TASK_CC = AGENT_TASK.replace(_AG_STEPS_34, _CC_STEPS_34)

# Appended to Claude Code's own system prompt (--append-system-prompt). Only facts about
# the sandbox; no advice on how to work.
AGENT_CC_APPEND = """\
The working directory contains program.dfy. Dafny 4.11 is installed (`dafny`). There is no \
internet access.\
"""

# Harness instructions, sent as the system message of agent runs (1c-run_agent.py);
# AGENT_TASK is the first user message. They follow mini-SWE-agent's protocol: one
# shell command per turn, each in a fresh subshell.
AGENT_PROTOCOL = """\
You are working in a terminal. Each of your responses must contain a short explanation \
of your reasoning followed by exactly one shell command in a ```bash code block. The \
command runs in a new shell in the directory that contains program.dfy, so directory \
changes and environment variables do not persist between commands. Edit files with \
non-interactive tools (for example a here-document, sed or python3); editors and pagers \
are not available. Dafny itself is not installed; two commands run it on program.dfy. \
`resolve` checks syntax and types without verifying, and can be run any number of times. \
`verify` runs the verifier and can be run at most {verify_limit} times. Run each of them \
as a command on its own, not combined with other commands. You can run at most \
{step_limit} commands in total. When you are done, respond with the command \
`echo TASK_COMPLETE`.\
"""

AGENT_FORMAT_ERROR = """\
Your response must contain exactly one shell command in a ```bash code block \
(found {n}). Respond again with one command.\
"""

AGENT_VERIFY_PASS = "Verification succeeded: Dafny verified program.dfy."
AGENT_VERIFY_FAIL = """\
Verification failed.

Dafny's error output:
{error_message}

Counterexample:
{counterexample_nl}

verify can be run {remaining} more time(s).\
"""
AGENT_VERIFY_TIMEOUT = "Verification failed: Dafny timed out after {timeout} seconds.\n\nverify can be run {remaining} more time(s)."
AGENT_VERIFY_LIMIT   = "verify has already been run {verify_limit} times and cannot be run again."
AGENT_VERIFY_ALONE   = "Run `verify` as a command on its own, not combined with other commands or arguments."
AGENT_RESOLVE_PASS   = "Resolution succeeded: program.dfy has no syntax or type errors. It has not been verified."
AGENT_RESOLVE_FAIL   = "Resolution failed.\n\n{output}"
AGENT_RESOLVE_TIMEOUT = "Resolution failed: Dafny timed out after {timeout} seconds."

INTENT_SECTION         = "=== INTENDED BEHAVIOR ===\n{intent}\n\n"

# Opens every prompt except ss_nodafnyref. Identical in every condition and mode, so it
# is a cacheable prefix; the task that follows is unchanged.
REFERENCE_INTRO   = "Below is a reference on the Dafny language and verifier, followed by your task."
REFERENCE_END     = "=== END OF DAFNY REFERENCE ===\n\n"
REFERENCE_SECTION = REFERENCE_INTRO + "\n\n=== DAFNY REFERENCE ===\n{reference}\n" + REFERENCE_END


# ─── helpers ──────────────────────────────────────────────────────────────────

def mask_error_message(error_message: str) -> str:
    """
    Hide what each error says while keeping where it is:
      program.dfy(12,4): Error: this loop invariant could not be proved on entry
        -> program.dfy(12,4): Error: verification failed
      program.dfy(9,12): Related location: this is the postcondition that could not be proved
        -> program.dfy(9,12): Related location
    Code-snippet lines (with the ^^^ marker) and the summary line are kept.
    """
    out = []
    for line in error_message.splitlines():
        line = re.sub(r"(\.dfy\(\d+,\d+\)):\s*Error:.*", r"\1: Error: verification failed", line)
        line = re.sub(r"(\.dfy\(\d+,\d+\)):\s*Related location:.*", r"\1: Related location", line)
        line = re.sub(r"(Related message:).*", r"\1", line)
        out.append(line)
    return "\n".join(out)


def dafny_reference() -> str:
    """DAFNY.md without its YAML front matter (the skill header)."""
    text = DAFNY_REF.read_text()
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip()


def intent_description(item: dict) -> str:
    """The reviewed intended-behavior description for the item's program."""
    if not INTENT_FILE.exists():
        raise FileNotFoundError(f"{INTENT_FILE} not found; write the intent descriptions first.")
    entry = json.loads(INTENT_FILE.read_text())["programs"].get(item["base_program"])
    if not entry or not entry.get("description") or not entry.get("reviewed"):
        raise ValueError(f"No reviewed intent description for {item['base_program']}")
    return entry["description"].strip()


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"Expected exactly one occurrence of {old[:60]!r} in the prompt")
    return text.replace(old, new)


# ─── builders ─────────────────────────────────────────────────────────────────

def with_reference(prompt: str) -> str:
    """The Dafny reference followed by the task prompt."""
    return REFERENCE_SECTION.format(reference=dafny_reference()) + prompt


def split_reference(prompt: str) -> tuple[str, str] | None:
    """(reference section, rest) for a prompt built by with_reference, else None.
    Used to put a cache breakpoint after the reference (Claude)."""
    if not prompt.startswith(REFERENCE_INTRO):
        return None
    cut = prompt.index(REFERENCE_END) + len(REFERENCE_END)
    return prompt[:cut], prompt[cut:]


def build_single_shot(item: dict, condition: str) -> str:
    """Prompt for one single-shot condition."""
    main = item["prompt"]
    if condition in ("ss_main", "ss_nodafnyref"):
        body = main
    elif condition == "ss_masked":
        error_block = f"=== DAFNY ERROR ===\n{item['error_message']}\n"
        body = _replace_once(main, error_block,
                             f"=== DAFNY ERROR ===\n{mask_error_message(item['error_message'])}\n")
    elif condition == "ss_intent":
        body = _replace_once(main, PROGRAM_HEADER,
                             INTENT_SECTION.format(intent=intent_description(item)) + PROGRAM_HEADER)
    else:
        raise ValueError(f"{condition} is not a single-shot condition")
    return body if condition == "ss_nodafnyref" else with_reference(body)


def build_iterative_followup(error_message: str = None, counterexample_nl: str = None,
                             unapplied_reason: str = None, source: str = None) -> str:
    """Follow-up turn for it_main: verifier feedback, or why the answer couldn't be applied."""
    if unapplied_reason:
        return ITERATIVE_UNAPPLIED.format(reason=UNAPPLIED_REASONS[unapplied_reason])
    return ITERATIVE_FEEDBACK.format(source=source, error_message=error_message,
                                     counterexample_nl=counterexample_nl)


def build_agent_task(item: dict, condition: str) -> str:
    """Task prompt for an agentic condition (same text for both harnesses)."""
    if condition not in ("ag_main", "ag_main_cc"):
        raise ValueError(f"{condition} is not an agentic condition")
    task = AGENT_TASK_CC if condition == "ag_main_cc" else AGENT_TASK
    return with_reference(task.format(error_message=item["error_message"],
                                      counterexample_nl=item["counterexample_nl"]))
