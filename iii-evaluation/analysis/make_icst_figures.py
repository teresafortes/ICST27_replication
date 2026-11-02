#!/usr/bin/env python3
"""
make_icst_figures.py  —  Figures for the ICST'27 paper (single-column, PDF + PNG).

Model colours are the logos' brand colours and every model is labelled with its logo
(icst_style.py). No titles inside the images: the LaTeX caption carries the title.

  fig_fidelity.pdf    what verifying repairs do to the specification, by model and mode:
                      exact / equivalent / weaker / stronger / other / does not verify
  fig_iterative.pdf   cumulative pass rate by attempt (it_main, k <= 5)
  fig_effects.pdf     paired change vs. single-shot main for every variation and mode,
                      with 95% CIs and the ±5 pp smallest effect of interest
  fig_pass_faithful.pdf pass rate vs. faithful pass rate by model (colour) and mode (shade)
  fig_diag_repair.pdf pass rate when the diagnosis is right vs. wrong, by model and mode,
                      with the number of misdiagnosed items per row

Usage:
  python3 iii-evaluation/analysis/make_icst_figures.py
  python3 iii-evaluation/analysis/make_icst_figures.py --scores-dir DIR --tables-dir DIR --demo
"""
from __future__ import annotations
import argparse, json, sys, warnings
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.lines import Line2D

sys.path.insert(0, str(Path(__file__).resolve().parent))
import icst_style as st                      # noqa: E402
from load_scores import load_all              # noqa: E402

warnings.filterwarnings("ignore")
EVAL = Path(__file__).resolve().parents[1]
DPI = 300
COL_W = 3.45          # IEEE single column, inches


def logo_zoom(height_pt: float) -> float:
    """OffsetImage zoom that draws a 256-px logo at height_pt points (zoom is points per
    image pixel; matplotlib applies the figure dpi itself)."""
    return height_pt / 256


def save(fig, out: Path, name: str, demo: bool):
    if demo:
        st.watermark(fig)
    out.mkdir(parents=True, exist_ok=True)
    fig.savefig(out / f"{name}.pdf", dpi=DPI)
    fig.savefig(out / f"{name}.png", dpi=DPI)
    plt.close(fig)
    print(f"  wrote {out / name}.pdf")


def subset_items(n: int) -> set:
    order = json.loads((st.ROOT / "ii-corpus" / "mode_subset.json").read_text())["order"]
    return set(order[:n])


def rows_for(r1: pd.DataFrame, key: str, mode: str) -> pd.DataFrame:
    if key == st.CC_KEY:
        return r1[(r1.condition == "ag_main_cc") & (r1.model == "claude-opus-5-5")]
    return r1[(r1.condition == st.MAIN_OF_MODE[mode]) & (r1.model == key)]


# ─── Figure: fidelity composition ─────────────────────────────────────────────

def fig_fidelity(r1, out, subset, demo):
    items = subset_items(subset)
    r = r1[r1.item_id.isin(items)].copy()
    cat = np.where(~r["pass"].astype(bool), "fail", r["fidelity"].fillna("other"))
    r["cat"] = pd.Series(cat, index=r.index).replace({"neither": "other", "undetermined": "other"})
    bars = []                                   # (key, mode, shares)
    for key in st.MODELS:
        for mode in st.MODES:
            g = rows_for(r, key, mode)
            if len(g):
                bars.append((key, mode, g["cat"].value_counts(normalize=True)))
    g = rows_for(r, st.CC_KEY, "agentic")
    if len(g):
        bars.append((st.CC_KEY, "agentic", g["cat"].value_counts(normalize=True)))
    if not bars:
        print("  fig_fidelity: no data, skipped")
        return

    # y positions: 3 bars per model, a gap between models
    ys, y, prev = [], 0.0, None
    for key, mode, _ in bars:
        if prev is not None and key != prev:
            y += 0.55
        ys.append(y)
        y += 1
        prev = key
    ys = np.array(ys)
    fig, ax = plt.subplots(figsize=(COL_W, 0.24 * len(bars) + 0.75), dpi=DPI)
    for (key, mode, shares), yy in zip(bars, ys):
        left = 0.0
        for c, label, color in st.FIDELITY:
            w = float(shares.get(c, 0.0))
            if w <= 0:
                continue
            ax.barh(yy, w, left=left, height=0.78, color=color,
                    edgecolor="white" if c != "fail" else "#CFCFCF", linewidth=0.5)
            if c in ("exact", "weaker") and w >= 0.07:
                ax.text(left + w / 2, yy, f"{100 * w:.0f}", ha="center", va="center", fontsize=6.3,
                        color="white", weight="bold")
            left += w
        passed = 1 - float(shares.get("fail", 0.0))
        ax.plot([passed, passed], [yy - 0.39, yy + 0.39], color="#333333", lw=0.8)
    ax.set_yticks(ys)
    ax.set_yticklabels([st.MODE_NAME[m] for k, m, _ in bars])   # the model name is under the logo
    ax.invert_yaxis()
    ax.set_xlim(0, 1)
    ax.set_xticks(np.linspace(0, 1, 6))
    ax.set_xticklabels([f"{int(x)}%" for x in np.linspace(0, 100, 6)])
    ax.set_xlabel("Share of repairs (the black tick marks the pass rate)")
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0, pad=4)
    # logo per model group, left of the tick labels
    for key in dict.fromkeys(k for k, _, _ in bars):
        yy = ys[[i for i, (k, _, _) in enumerate(bars) if k == key]].mean()
        st.add_logo(ax, key, (-0.255, yy - 0.25), zoom=logo_zoom(14), xycoords=("axes fraction", "data"))
        ax.text(-0.255, yy + 0.55, st.NAME[key], transform=ax.get_yaxis_transform(), ha="center", va="center",
                fontsize=5.8, color="#444444")
    handles = [Patch(facecolor=c, edgecolor="#CFCFCF" if k == "fail" else c, label=l) for k, l, c in st.FIDELITY]
    ax.legend(handles=handles, ncol=3, loc="lower center", bbox_to_anchor=(0.42, 1.0), frameon=False,
              handlelength=1.0, columnspacing=0.9, handletextpad=0.4)
    save(fig, out, "fig_fidelity", demo)


# ─── Figure: pass rate vs. faithful pass rate ─────────────────────────────────

MODE_SHADE = {"single_shot": 0.30, "iterative": 0.62, "agentic": 1.0}   # tint toward the model colour


def tint(hex_color: str, strength: float):
    """Blend hex_color with white; strength 1 is the full colour."""
    from matplotlib.colors import to_rgb
    c = np.array(to_rgb(hex_color))
    return tuple(1 - strength * (1 - c))


def fig_pass_faithful(r1, out, subset, demo):
    items = subset_items(subset)
    r = r1[r1.item_id.isin(items)]
    pts = {}                                    # (key, mode) -> (faithful %, pass %)
    for key in st.MODELS + [st.CC_KEY]:
        for mode in (["agentic"] if key == st.CC_KEY else st.MODES):
            g = rows_for(r, key, mode)
            if len(g):
                pts[(key, mode)] = (100 * g["faithful_pass"].mean(), 100 * g["pass"].mean())
    if not pts:
        print("  fig_pass_faithful: no data, skipped")
        return
    lo, hi = 85, 101
    fig, ax = plt.subplots(figsize=(COL_W, 2.3), dpi=DPI)
    ax.plot([lo, hi], [lo, hi], color="#BBBBBB", lw=0.8, zorder=1)
    ax.text(96.2, 96.6, "pass = faithful", rotation=45, rotation_mode="anchor", transform_rotates_text=True,
            fontsize=6, color="#888888", ha="left", va="bottom")
    for key in st.MODELS:
        path = [pts[(key, m)] for m in st.MODES if (key, m) in pts]
        if len(path) > 1:
            xs, ys = zip(*path)
            ax.plot(xs, ys, color=st.COLOR[key], lw=0.8, alpha=0.45, zorder=2)
        for mode in st.MODES:
            if (key, mode) in pts:
                x, y = pts[(key, mode)]
                ax.plot(x, y, "o", ms=6, mfc=tint(st.COLOR[key], MODE_SHADE[mode]),
                        mec=st.COLOR[key], mew=0.9, zorder=3)
    if (st.CC_KEY, "agentic") in pts:
        x, y = pts[(st.CC_KEY, "agentic")]
        ax.plot(x, y, "s", ms=5.5, mfc=st.COLOR["claude-opus-5-5"], mec="#333333", mew=0.9, zorder=3)
        ax.annotate("Claude Code", (x, y), xytext=(0, -9), textcoords="offset points",
                    ha="center", va="top", fontsize=6.3, color="#444444")
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ticks = np.arange(lo, 101, 5)
    ax.set_xticks(ticks)
    ax.set_yticks(ticks)
    ax.set_xlabel("Faithful pass rate (%)")
    ax.set_ylabel("Pass rate (%)")
    ax.grid(True)
    model_leg = ax.legend([st.LogoHandle(k) for k in st.MODELS], [st.NAME[k] for k in st.MODELS],
                          handler_map=st.logo_handler_map(), loc="lower right", bbox_to_anchor=(1.0, 0.0),
                          frameon=True, facecolor="white", edgecolor="none", framealpha=1, handlelength=2.2, handletextpad=0.5, borderaxespad=0.3)
    ax.add_artist(model_leg)
    grey = "#555555"
    mode_handles = [Line2D([], [], ls="", marker="o", ms=6, mfc=tint(grey, MODE_SHADE[m]), mec=grey, mew=0.9)
                    for m in st.MODES]
    ax.legend(mode_handles, [st.MODE_NAME[m] for m in st.MODES], loc="lower right",
              bbox_to_anchor=(0.56, 0.0), frameon=True,
              facecolor="white", edgecolor="none", framealpha=1, handletextpad=0.4, borderaxespad=0.3)
    save(fig, out, "fig_pass_faithful", demo)


# ─── Figure: iterative curve ──────────────────────────────────────────────────

def fig_iterative(tdir, out, demo):
    p = tdir / "iterative_curve.csv"
    if not p.exists():
        print("  fig_iterative: iterative_curve.csv missing, skipped")
        return
    c = pd.read_csv(p)
    fig, ax = plt.subplots(figsize=(COL_W, 1.9), dpi=DPI)
    kmax = 5
    for key in st.MODELS:
        g = c[(c.condition == "it_main") & (c.model == key)].sort_values("attempt")
        if g.empty:
            continue
        ax.plot(g.attempt, 100 * g.cumulative_pass, "-o", color=st.COLOR[key], ms=3.2, lw=1.4, zorder=3)
    ax.set_xticks(range(1, kmax + 1))
    ax.set_xlim(0.7, kmax + 0.3)
    ax.set_ylim(80, 101)
    ax.set_yticks(range(80, 101, 5))
    ax.set_xlabel("Attempt")
    ax.set_ylabel("Cumulative pass rate (%)")
    ax.grid(axis="y")
    keys = [k for k in st.MODELS if ((c.condition == "it_main") & (c.model == k)).any()]
    ax.legend([st.LogoHandle(k, line=True) for k in keys], [st.NAME[k] for k in keys],
              handler_map=st.logo_handler_map(), loc="lower right", bbox_to_anchor=(1.0, 0.02), frameon=False, handlelength=2.6,
              handletextpad=0.5, borderaxespad=0.2)
    save(fig, out, "fig_iterative", demo)


# ─── Figure: effects (forest plot) ────────────────────────────────────────────

EFFECT_ROWS = [  # (group label, variation, metric, row label)
    ("RQ1", "ss_masked", "obligation_correct", "Masked message: obligation"),
    ("RQ4", "ss_nodafnyref", "spec_correct", "No Dafny ref.: element"),
    ("RQ4", "ss_nodafnyref", "pass", "No Dafny ref.: pass"),
    ("RQ4", "ss_intent", "pass", "Intent: pass"),
    ("RQ4", "ss_intent", "faithful_pass", "Intent: faithful pass"),
    ("RQ3", "it_main", "pass", "Iterative: pass"),
    ("RQ3", "it_main", "faithful_pass", "Iterative: faithful pass"),
    ("RQ3", "it_main", "weaker", "Iterative: weaker repair"),
    ("RQ3", "ag_main", "pass", "Agentic: pass"),
    ("RQ3", "ag_main", "faithful_pass", "Agentic: faithful pass"),
    ("RQ3", "ag_main", "weaker", "Agentic: weaker repair"),
]


def fig_effects(tdir, out, demo):
    p = tdir / "mcnemar.csv"
    if not p.exists():
        print("  fig_effects: mcnemar.csv missing, skipped")
        return
    t = pd.read_csv(p)
    t = t[t.reference == "ss_main"]
    rows = [r for r in EFFECT_ROWS if ((t.variation == r[1]) & (t.metric == r[2])).any()]
    fig, ax = plt.subplots(figsize=(COL_W, 0.27 * len(rows) + 0.7), dpi=DPI)
    off = {k: o for k, o in zip(st.MODELS, (-0.22, 0.0, 0.22))}
    y, ys, prev = 0.0, [], None
    for grp, var, met, lab in rows:
        if prev is not None and grp != prev:
            y += 0.5
            ax.axhline(y - 0.75, color="#DDDDDD", lw=0.6)
        ys.append(y)
        for key in st.MODELS:
            r = t[(t.variation == var) & (t.metric == met) & (t.model == key)]
            if r.empty:
                continue
            r = r.iloc[0]
            yy = y + off[key]
            sig = bool(r.significant) and bool(r.get("cluster_agrees", True))
            ax.plot([100 * r.ci_low, 100 * r.ci_high], [yy, yy], color=st.COLOR[key], lw=1.1)
            ax.plot(100 * r["diff"], yy, "o", ms=3.6, color=st.COLOR[key],
                    markerfacecolor=st.COLOR[key] if sig else "white", markeredgewidth=1.0, zorder=3)
        y += 1
        prev = grp
    ax.axvspan(-5, 5, color="#F1F1F1", zorder=0)
    ax.axvline(0, color="#555555", lw=0.7)
    ax.set_yticks(ys)
    ax.set_yticklabels([r[3] for r in rows])
    ax.invert_yaxis()
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    ax.set_xlabel("Change vs. single-shot main (percentage points, 95% CI)")
    ax.grid(axis="x")
    # legend with logos: draw logo + name in the legend row above the plot
    handles = [st.LogoHandle(k) for k in st.MODELS]
    handles += [Line2D([], [], marker="o", ls="", color="#666666", markerfacecolor="white", ms=4),
                Patch(facecolor="#F1F1F1", label="±5 pp")]
    labels = [st.NAME[k] for k in st.MODELS] + ["not significant", "±5 pp"]
    ax.legend(handles, labels, ncol=3, loc="lower center", bbox_to_anchor=(0.35, 1.0), frameon=False,
              handletextpad=0.4, columnspacing=1.0, handlelength=2.4, handler_map=st.logo_handler_map())
    save(fig, out, "fig_effects", demo)


# ─── Figure: diagnosis and repair ─────────────────────────────────────────────

def fig_diag_repair(r1, tdir, out, subset, demo):
    items = subset_items(subset)
    r = r1[r1.item_id.isin(items)]
    pts = []
    for key in st.MODELS:
        for mode in st.MODES:
            g = rows_for(r, key, mode)
            if g.empty:
                continue
            col = "spec_correct" if mode == "single_shot" else "spec_correct_last"
            ok = g[col].astype(bool)
            pts.append((key, mode, g.loc[ok, "pass"].mean(), g.loc[~ok, "pass"].mean(), int((~ok).sum())))
    if not pts:
        print("  fig_diag_repair: no data, skipped")
        return
    fig, ax = plt.subplots(figsize=(COL_W, 0.24 * len(pts) + 0.8), dpi=DPI)
    y, ys, prev = 0.0, [], None
    for key, mode, p_ok, p_bad, n_bad in pts:
        if prev is not None and key != prev:
            y += 0.5
        ys.append(y)
        ax.plot([100 * p_bad, 100 * p_ok], [y, y], color="#C9C9C9", lw=2.2, solid_capstyle="round", zorder=1)
        ax.plot(100 * p_bad, y, "o", ms=4.5, color="white", markeredgecolor=st.COLOR[key], mew=1.2, zorder=2)
        ax.plot(100 * p_ok, y, "o", ms=4.5, color=st.COLOR[key], zorder=3)
        # few items are misdiagnosed, so give their count next to the hollow point
        x_lab = 100 * (p_bad if n_bad else p_ok)
        if x_lab < 15:      # near 0 the label would hit the axis labels: put it above the line instead
            ax.text(x_lab + 2, y - 0.18, f"{n_bad} wrong", ha="left", va="bottom", fontsize=6, color="#666666")
        else:
            ax.text(x_lab - 3, y, f"{n_bad} wrong", ha="right", va="center", fontsize=6, color="#666666")
        y += 1
        prev = key
    ys = np.array(ys)
    ax.set_yticks(ys)
    ax.set_yticklabels([st.MODE_NAME[m] for _, m, *_ in pts])
    ax.invert_yaxis()
    ax.tick_params(axis="y", length=0, pad=4)
    ax.spines["left"].set_visible(False)
    ax.set_xlabel("Pass rate (%)")
    ax.grid(axis="x")
    for key in dict.fromkeys(k for k, *_ in pts):
        yy = ys[[i for i, p in enumerate(pts) if p[0] == key]].mean()
        st.add_logo(ax, key, (-0.225, yy - 0.25), zoom=logo_zoom(14), xycoords=("axes fraction", "data"))
        ax.text(-0.225, yy + 0.55, st.NAME[key], transform=ax.get_yaxis_transform(), ha="center", va="center",
                fontsize=5.8, color="#444444")
    handles = [Line2D([], [], marker="o", ls="", color="#666666", ms=4.5),
               Line2D([], [], marker="o", ls="", color="white", markeredgecolor="#666666", ms=4.5)]
    ax.legend(handles, ["element diagnosed correctly", "diagnosed incorrectly"], ncol=1, loc="lower left",
              bbox_to_anchor=(-0.02, 1.0), frameon=False, handletextpad=0.3, labelspacing=0.25)
    save(fig, out, "fig_diag_repair", demo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scores-dir", default=None, help="Default: iii-evaluation/scores")
    ap.add_argument("--tables-dir", default=str(EVAL / "analysis" / "tables" / "conditions"))
    ap.add_argument("--out-dir", default=str(st.PAPER / "figures"))
    ap.add_argument("--subset", type=int, default=200)
    ap.add_argument("--demo", action="store_true", help="Watermark figures as synthetic")
    args = ap.parse_args()
    st.mpl_setup()
    out, tdir = Path(args.out_dir), Path(args.tables_dir)
    data = load_all(Path(args.scores_dir) if args.scores_dir else None, verbose=False)
    r1 = data.long[data.long["run"].astype(int) == 1]
    fig_fidelity(r1, out, args.subset, args.demo)
    fig_pass_faithful(r1, out, args.subset, args.demo)
    fig_iterative(tdir, out, args.demo)
    fig_effects(tdir, out, args.demo)
    fig_diag_repair(r1, tdir, out, args.subset, args.demo)


if __name__ == "__main__":
    main()
