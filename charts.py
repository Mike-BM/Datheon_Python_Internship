from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

BG_COLOR = "#fcfcfb"
TEXT_COLOR = "#0b0b0b"
MUTED_COLOR = "#52514e"
GRID_COLOR = "#e4e3df"
COLOR_GAIN = "#2a78d6"
COLOR_LOSS = "#e34948"
COLOR_BAR = "#2a78d6"


def _style(ax) -> None:
    ax.set_facecolor(BG_COLOR)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID_COLOR)
    ax.tick_params(colors=MUTED_COLOR, length=0, labelsize=10)
    ax.grid(axis="x", color=GRID_COLOR, linewidth=0.8)
    ax.set_axisbelow(True)


def _headline(fig, ax, title: str, subtitle: str) -> None:
    ax.set_title(title, loc="left", fontsize=13, fontweight="bold",
                 color=TEXT_COLOR, pad=26)
    ax.text(0, 1.03, subtitle, transform=ax.transAxes, fontsize=10,
            color=MUTED_COLOR, va="bottom")


def _performance_title(holdings: list) -> str:
    ranked = sorted(holdings, key=lambda h: h.gain_loss_pct, reverse=True)
    best, worst = ranked[0], ranked[-1]
    winners = sum(1 for h in holdings if h.gain_loss_pct > 0)
    if winners == len(holdings):
        return f"All {len(holdings)} holdings in profit; {best.ticker} leads at {best.gain_loss_pct:+.1f}%"
    if winners == 0:
        return f"All {len(holdings)} holdings at a loss; {worst.ticker} lowest at {worst.gain_loss_pct:+.1f}%"
    return f"{winners} of {len(holdings)} holdings in profit; {best.ticker} leads, {worst.ticker} lags"


def performance_chart(holdings: list, totals: dict, out_dir: Path) -> Path:
    ranked = sorted(holdings, key=lambda h: h.gain_loss_pct, reverse=True)
    values = [h.gain_loss_pct for h in ranked]
    labels = [h.ticker for h in ranked]
    colors = [COLOR_GAIN if v >= 0 else COLOR_LOSS for v in values]

    fig, ax = plt.subplots(
        figsize=(9, max(3.4, 0.46 * len(ranked) + 1.9)),
        facecolor=BG_COLOR,
    )
    _style(ax)
    ax.barh(labels, values, color=colors, height=0.58)
    ax.invert_yaxis()
    ax.axvline(0, color=MUTED_COLOR, linewidth=1)

    span = (max(values + [0]) - min(values + [0])) or 1
    pad = span * 0.02
    for y, v in enumerate(values):
        ax.text(
            v + (pad if v >= 0 else -pad),
            y,
            f"{v:+.1f}%",
            va="center",
            ha="left" if v >= 0 else "right",
            fontsize=10,
            color=TEXT_COLOR,
        )
    ax.set_xlim(min(values + [0]) - span * 0.14, max(values + [0]) + span * 0.14)
    ax.xaxis.set_major_formatter(lambda x, _: f"{x:+.0f}%" if x else "0%")

    _headline(
        fig,
        ax,
        _performance_title(holdings),
        f"Gain/loss versus buy price. Portfolio {totals['pct']:+.1f}% "
        f"({totals['gain']:+,.0f} on {totals['cost']:,.0f} invested)",
    )
    path = Path(out_dir) / "performance_by_holding.png"
    fig.savefig(path, dpi=200, bbox_inches="tight", facecolor=BG_COLOR)
    plt.close(fig)
    return path


def sector_chart(sectors: list, totals: dict, out_dir: Path) -> Path:
    names = [s[0] for s in sectors]
    weights = [s[2] for s in sectors]

    fig, ax = plt.subplots(
        figsize=(9, max(3.0, 0.5 * len(names) + 1.9)),
        facecolor=BG_COLOR,
    )
    _style(ax)
    ax.barh(names, weights, color=COLOR_BAR, height=0.58)
    ax.invert_yaxis()
    for y, w in enumerate(weights):
        ax.text(
            w + max(weights) * 0.015,
            y,
            f"{w:.1f}%",
            va="center",
            fontsize=10,
            color=TEXT_COLOR,
        )
    ax.set_xlim(0, max(weights) * 1.15)
    ax.xaxis.set_major_formatter(lambda x, _: f"{x:.0f}%")

    _headline(
        fig,
        ax,
        f"{names[0]} is the largest sector at {weights[0]:.0f}% of portfolio value",
        f"Share of current market value by GICS sector. Total value {totals['value']:,.0f}",
    )
    path = Path(out_dir) / "sector_allocation.png"
    fig.savefig(path, dpi=200, bbox_inches="tight", facecolor=BG_COLOR)
    plt.close(fig)
    return path


def make_charts(holdings: list, totals: dict, sectors: list, out_dir) -> list:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    return [performance_chart(holdings, totals, out), sector_chart(sectors, totals, out)]
