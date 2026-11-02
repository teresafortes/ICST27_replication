"""Shared look for the ICST'27 tables and figures (make_icst_tables.py, make_icst_figures.py).

Model colours are the logos' brand colours, so a bar or line matches the logo next to it.
Logos live in iii-evaluation/analysis/logos/.
"""
from __future__ import annotations
from pathlib import Path

ROOT     = Path(__file__).resolve().parents[2]
PAPER    = ROOT / "iii-evaluation" / "analysis" / "paper_assets"   # tables/, figures/, icst_macros.tex
LOGO_DIR = Path(__file__).resolve().parent / "logos"

MODELS = ["claude-opus-5-5", "deepseek-v4-pro-0813", "qwen3.8-27b"]
# (model, condition) -> display key; Claude Code is its own row
CC_KEY = "claude-code"

NAME = {"claude-opus-5-5": "Opus 5.5", "deepseek-v4-pro-0813": "DeepSeek-V4-Pro",
        "qwen3.8-27b": "Qwen3.8-27B", CC_KEY: "Claude Code"}
# LaTeX macro (defined in icst_macros.tex) that prints logo + name
TEX = {"claude-opus-5-5": r"\opus", "deepseek-v4-pro-0813": r"\deepseek",
       "qwen3.8-27b": r"\qwen", CC_KEY: r"\claudecode"}
LOGO = {"claude-opus-5-5": "claude.png", "deepseek-v4-pro-0813": "deepseek.png",
        "qwen3.8-27b": "qwen.png", CC_KEY: "claudecode.png"}
COLOR = {"claude-opus-5-5": "#D97757", "deepseek-v4-pro-0813": "#4D6BFE",
         "qwen3.8-27b": "#9B7CF0", CC_KEY: "#B4532A"}

MODES = ["single_shot", "iterative", "agentic"]
MODE_NAME = {"single_shot": "Single-shot", "iterative": "Iterative", "agentic": "Agentic"}
MAIN_OF_MODE = {"single_shot": "ss_main", "iterative": "it_main", "agentic": "ag_main"}

# Repair-oracle categories, in stacking order. Faithful outcomes are teal, weakening is
# the one saturated warm colour, everything else is muted.
FIDELITY = [
    ("exact",        "Exact",        "#1F6F63"),
    ("equivalent",   "Equivalent",   "#6DBFAE"),
    ("weaker",       "Weaker",       "#D1495B"),
    ("stronger",     "Stronger",     "#F0B44C"),
    ("other",        "Other",        "#B9B4C7"),
    ("fail",         "Does not verify", "#EDEDED"),
]

# Table heat colours (as in the ICLR'27 tables): blue-violet for "higher is better",
# red for quantities where higher is worse (weakening). Defined in icst_macros.tex.
HEAT_GOOD, HEAT_BAD = "heatgood", "heatbad"
HEAT_MAX = 45          # maximum \cellcolor intensity (percent of the heat colour)


def mpl_setup():
    import matplotlib as mpl
    mpl.rcParams.update({
        "font.family": "serif", "font.serif": ["STIXGeneral", "Times New Roman", "DejaVu Serif"],
        "mathtext.fontset": "stix", "font.size": 8, "axes.labelsize": 8,
        "xtick.labelsize": 7.5, "ytick.labelsize": 7.5, "legend.fontsize": 7,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": "#444444", "axes.linewidth": 0.6,
        "xtick.major.width": 0.6, "ytick.major.width": 0.6,
        "xtick.color": "#333333", "ytick.color": "#333333",
        "grid.color": "#E3E3E3", "grid.linewidth": 0.6,
        "pdf.fonttype": 42, "savefig.bbox": "tight", "savefig.pad_inches": 0.02,
    })


def logo_image(key: str):
    import matplotlib.image as mpimg
    return mpimg.imread(LOGO_DIR / LOGO[key])


def add_logo(ax, key: str, xy, zoom: float = 0.045, xycoords="data", box_alignment=(0.5, 0.5)):
    """Place a model logo at xy (data or axes coordinates)."""
    from matplotlib.offsetbox import OffsetImage, AnnotationBbox
    ab = AnnotationBbox(OffsetImage(logo_image(key), zoom=zoom), xy, xycoords=xycoords,
                        frameon=False, box_alignment=box_alignment, annotation_clip=False)
    ax.add_artist(ab)
    return ab


def watermark(fig, text: str = "SYNTHETIC DATA"):
    fig.text(0.5, 0.5, text, fontsize=26, color="#D1495B", alpha=0.18, rotation=25,
             ha="center", va="center", weight="bold", zorder=100)


class LogoHandle:
    """Legend handle for a model: draw it with HandlerLogo (dot in the model colour + logo)."""
    def __init__(self, key: str, marker: bool = True, line: bool = False):
        self.key, self.marker, self.line = key, marker, line


def logo_handler_map():
    from matplotlib.legend_handler import HandlerBase
    from matplotlib.lines import Line2D
    from matplotlib.image import BboxImage
    from matplotlib.transforms import Bbox, TransformedBbox

    class HandlerLogo(HandlerBase):
        def create_artists(self, legend, h, x0, y0, width, height, fontsize, trans):
            arts = []
            cy = y0 + height / 2
            if h.line:
                arts.append(Line2D([x0, x0 + width * 0.45], [cy, cy], color=COLOR[h.key], lw=1.4,
                                   transform=trans))
            if h.marker:
                arts.append(Line2D([x0 + width * 0.22], [cy], marker="o", ls="", ms=4,
                                   color=COLOR[h.key], transform=trans))
            s = height * 1.35
            lx = x0 + width * 0.55
            bb = Bbox.from_bounds(lx, cy - s / 2, s, s)
            img = BboxImage(TransformedBbox(bb, trans), interpolation="antialiased")
            img.set_data(logo_image(h.key))
            arts.append(img)
            return arts

    return {LogoHandle: HandlerLogo()}
