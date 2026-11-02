# Replication package: Diagnosing and Repairing Dafny Specification Failures with LLMs

This package contains the benchmark, the code, the model outputs and the analysis for
the paper. With it you can:

1. **Re-create every table and figure** from the stored scores, with no API calls and
   no Dafny (a few seconds).
2. **Re-score** every stored model response with Dafny (diagnosis, verification and
   faithfulness oracle).
3. **Re-run the experiments** with your own API keys.
4. **Rebuild the benchmark** from the verified DafnyBench programs.

## Layout

```
i-corpus-construction/      benchmark construction
  1-generate_mutations.py     inject one fault per program x task x operator (CD, BS, CA);
                              keep mutants that Dafny rejects and whose direction is confirmed
  2-collect_verifier_output.py  run Dafny on each mutant; store errors, counterexamples,
                              obligation labels and prompts -> ii-corpus/benchmark_full.json
  3-build_intent_descriptions.py  build the intended-behavior descriptions (ss_intent)
  4-sample_evaluation_set.py  draw the 600-item evaluation set (deterministic)
  5-order_mode_subset.py      fixed stratified order; first 200 items = iterative/agent subset
  spec_polarity.py            polarity of bounds, used to choose the bound-shift direction
  leak_judge.py               optional LLM check of the intent descriptions (not reported)
  intent_written.json         descriptions written for the programs without an upstream one

ii-corpus/                  the benchmark
  benchmark_full.json         full pool: 1,117 items from 308 programs
  benchmark.json              evaluation set: 600 items (used in the paper)
  {low,medium,high}/*.dfy     original verified programs, by tier
  {low,medium,high}/{A,B,C}/  mutated programs (Task A: requires, B: invariant, C: ensures)
  corpus_manifest.csv         per-program tier and conjunct count
  mutation_log.json           every injection attempt and why it failed or succeeded
  intent_descriptions.json    intended-behavior descriptions (ss_intent)
  mode_subset.json            stratified order of the 600 items (first 200 = mode subset)
  consistency_sample.json     the 100 items run three times (run-to-run consistency)

iii-evaluation/             experiments and scoring
  prompts.py                  prompts for every condition
  agent_context/DAFNY.md      the Dafny reference given in every main condition
  1-run_evaluation.py         single-shot conditions (ss_main, ss_masked, ss_nodafnyref, ss_intent)
  1b-run_iterative.py         iterative mode (it_main, k <= 5; it_main_k10 pilot)
  1c-run_agent.py             agentic mode with mini-SWE-agent in a Docker sandbox (ag_main)
  1d-run_claude_code.py       Claude Code reference run (ag_main_cc)
  agent_sandbox/              Docker image, verify/resolve commands, pinned agent requirements
  agent_sandbox_cc/           Docker image and egress proxy for Claude Code
  2-score_diagnosis.py        parse answers; score element and obligation
  3-apply_repairs.py          apply each repair and run Dafny (oracle stage 1)
  4-check_semantic_equivalence.py  exact / equivalent / weaker / stronger / neither /
                              undetermined (oracle stages 2-3)
  validate_equivalence_check.py  checks the oracle on cases with known answers
  5-code_explanations.py      rule-based codes on the free-text diagnoses
  7-analyze_conditions.py     all statistics (McNemar + Holm, cluster bootstrap,
                              mixed-effects models, consistency, agent behavior)
  responses/<condition>/<model>/   raw model responses (one JSON per item and run)
  logs/agent_traj/            full agent trajectories (mini-SWE-agent and Claude Code)
  logs/*.log                  run logs
  scores/<condition>/         scores used in the paper
  scores_prefix/<condition>/  scores before the scorer refinement described in the paper's
                              footnote (multi-clause rules and agent edit flags)
  analysis/
    load_scores.py            loads all scores into one table
    make_icst_tables.py       paper tables   -> analysis/paper_assets/tables/
    make_icst_figures.py      paper figures  -> analysis/paper_assets/figures/
    tables/conditions/        every statistic reported in the paper (CSV)
    agent_review/             hand coding of the non-faithful agent and iterative passes
                              (nonfaithful_runs.csv, iterative_nonfaithful_runs.csv; the
                              diffs that were read are in nonfaithful_runs.md)
```

Conditions: `ss_main` (single-shot, main), `ss_masked` (verifier wording hidden),
`ss_nodafnyref` (no Dafny reference), `ss_intent` (intended-behavior description added),
`it_main` (iterative, k <= 5), `it_main_k10` (k = 10 pilot, 50 items), `ag_main` (agent),
`ag_main_cc` (Claude Code). Models: `claude-opus-5-5`, `deepseek-v4-pro-0813`,
`qwen3.8-27b`.

## Requirements

- Python 3.9+ with `pip install -r requirements.txt`
- Dafny 4.11.0 (`dotnet tool install --global dafny --version 4.11.0`) with Z3 4.15.4,
  for re-scoring and rebuilding
- Optional: R with `lme4` for the mixed-effects models (otherwise statsmodels is used)
- For the agentic runs: Docker, and Python 3.10+ for mini-SWE-agent 2.4.6
  (`pip install -r iii-evaluation/agent_sandbox/requirements.txt`; exact versions in
  `requirements.lock`)
- For new model runs: `API_KEY_ANTHROPIC_ICST` (Anthropic API) and
  `API_KEY_OPEN_ROUTER_ICST` (OpenRouter, for DeepSeek and Qwen)

## 1. Tables and figures from the stored scores (no API calls, no Dafny)

```bash
python3 iii-evaluation/7-analyze_conditions.py          # -> analysis/tables/conditions/
python3 iii-evaluation/analysis/make_icst_tables.py     # -> analysis/paper_assets/tables/
python3 iii-evaluation/analysis/make_icst_figures.py    # -> analysis/paper_assets/figures/
```

`--scores-dir iii-evaluation/scores_prefix` reproduces the numbers before the scorer
refinement.

## 2. Re-score the stored responses (Dafny, no API calls)

```bash
for c in ss_main ss_masked ss_nodafnyref ss_intent; do
  python3 iii-evaluation/2-score_diagnosis.py --condition $c
  python3 iii-evaluation/3-apply_repairs.py --condition $c
  python3 iii-evaluation/4-check_semantic_equivalence.py --condition $c
done
python3 iii-evaluation/1b-run_iterative.py --score
python3 iii-evaluation/4-check_semantic_equivalence.py --condition it_main
python3 iii-evaluation/1b-run_iterative.py --k 10 --limit 50 --score
python3 iii-evaluation/4-check_semantic_equivalence.py --condition it_main_k10
python3 iii-evaluation/1c-run_agent.py --condition ag_main --score
python3 iii-evaluation/1d-run_claude_code.py --score
python3 iii-evaluation/5-code_explanations.py --all
python3 iii-evaluation/validate_equivalence_check.py     # oracle sanity check
```

Then run the commands of step 1.

## 3. Re-run the experiments (paid API calls)

```bash
python3 iii-evaluation/1-run_evaluation.py --condition ss_main
python3 iii-evaluation/1-run_evaluation.py --condition ss_main --consistency   # runs 2-3, 100 items
python3 iii-evaluation/1-run_evaluation.py --condition ss_masked
python3 iii-evaluation/1-run_evaluation.py --condition ss_nodafnyref
python3 iii-evaluation/1-run_evaluation.py --condition ss_intent
python3 iii-evaluation/1b-run_iterative.py --subset 200
python3 iii-evaluation/1b-run_iterative.py --k 10 --limit 50                    # pilot

docker build -t dafny-agent-sandbox:2 iii-evaluation/agent_sandbox
python3 iii-evaluation/1c-run_agent.py --condition ag_main --subset 200        # Python 3.10+
docker build -t dafny-agent-sandbox-cc:1 iii-evaluation/agent_sandbox_cc
python3 iii-evaluation/1d-run_claude_code.py --subset 200
```

New runs write to `responses/`; move or rename the stored responses first. Then score
as in step 2. All model settings (reasoning effort, output limit, sampling) are in
`1-run_evaluation.py` and are shared by every mode.

## 4. Rebuild the benchmark

The original programs are the verified ("ground truth") programs of DafnyBench, stored in
`ii-corpus/{low,medium,high}/`.

```bash
python3 i-corpus-construction/1-generate_mutations.py
python3 i-corpus-construction/2-collect_verifier_output.py
python3 i-corpus-construction/4-sample_evaluation_set.py
python3 i-corpus-construction/5-order_mode_subset.py --report
python3 i-corpus-construction/3-build_intent_descriptions.py --sources DIR   # DIR: mbpp.jsonl, Clover-main/
```

Generation is seeded and deterministic. Verifier output can differ slightly with a
different Dafny or Z3 version.

## Data sources

The Dafny programs come from DafnyBench, which collects them from public repositories,
MBPP-DFY and CloverBench. They keep their original comments.
