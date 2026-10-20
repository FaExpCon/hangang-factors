"""Render derived portfolio PNGs from existing outputs only; never run models/APIs.

Requires an independently installed matplotlib. No source market-data/cache
files are read. Source result hashes and final normalized values are printed.
"""

import hashlib
import json
from datetime import date
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter


DESIGN = Path("outputs/registered_design_2016-07_2020-12_attempt2/runs")
VALIDATION = Path("outputs/frozen_validation_2021_2024")
DESTINATION = Path("docs/images")
COLORS = {"W-532 / resume": "#1769aa", "Eligible equal-weight / resume": "#bc6400", "KOSPI200 price index": "#585868"}


def read(path):
    raw = path.read_bytes()
    print(f"Source: {path}; SHA-256={hashlib.sha256(raw).hexdigest()}")
    return json.loads(raw)


def series(output, field="equity"):
    return [(date.fromisoformat(p["date"]), float(p[field])) for p in output]


def curves(chosen, eligible):
    result = {"W-532 / resume": series(chosen["equity"]),
              "Eligible equal-weight / resume": series(eligible["equity"]),
              "KOSPI200 price index": series(chosen["benchmarks"]["KOSPI200"], "close")}
    calendars = [tuple(d for d,v in observations) for observations in result.values()]
    if not all(c == calendars[0] for c in calendars):
        raise ValueError("Figure source calendars differ")
    return result


def annual(observations):
    years = sorted({d.year for d,v in observations})
    base = observations[0][1]
    result = {}
    for year in years:
        end = [v for d,v in observations if d.year == year][-1]
        result[year] = end/base - 1
        base = end
    return result


def style(ax):
    ax.set_facecolor("#f7f9fc")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=.23)
    ax.set_axisbelow(True)


def verify_labels(fig):
    """Bounded rendering check for titles/axis labels/legends; not visual review."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    artists = list(fig.texts)
    for ax in fig.axes:
        artists.extend([ax.title, ax.xaxis.label, ax.yaxis.label])
        if ax.get_legend(): artists.append(ax.get_legend())
    artists.extend(fig.legends)
    width,height = fig.canvas.get_width_height()
    for artist in artists:
        box = artist.get_window_extent(renderer)
        if box.width and box.height and (box.x0 < -1 or box.y0 < -1 or box.x1 > width+1 or box.y1 > height+1):
            raise ValueError("A plot title, axis label or legend exceeds the figure bounds")


def equity_figure(data, title, filename):
    fig, ax = plt.subplots(figsize=(11, 5.4), dpi=160)
    for name, observations in data.items():
        start = observations[0][1]
        normalized = [100*v/start for d,v in observations]
        ax.plot([d for d,v in observations], normalized, label=name, color=COLORS[name], linewidth=1.7)
        print(f"{filename}: {name}; final normalized equity={normalized[-1]:.6f}")
    style(ax)
    ax.axhline(100, color="#727982", linewidth=.7, linestyle="--")
    ax.set(title=title, ylabel="Normalized value (start = 100)", xlabel="Date")
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.legend(frameon=False, loc="upper left")
    fig.text(.06,.025,"Saved results only | Portfolio returns net of modeled costs; index is price-only | Dividends excluded",fontsize=8,color="#404852")
    fig.subplots_adjust(bottom=.16,top=.88,left=.09,right=.98)
    verify_labels(fig)
    fig.savefig(DESTINATION/filename, facecolor="white", metadata={"Description":"Derived normalized saved results; no raw provider data"})
    plt.close(fig)


def yearly_figure(design, validation):
    fig, axes = plt.subplots(1,2,figsize=(13,5.4),dpi=160,sharey=True)
    for ax,data,title in zip(axes,(design,validation),("Design: 2016 H2–2020", "Validation: 2021–2024")):
        years = list(annual(next(iter(data.values()))))
        for i,(name,observations) in enumerate(data.items()):
            returns=annual(observations)
            ax.bar([j+(i-1)*.24 for j in range(len(years))],[returns[y] for y in years],width=.23,
                   color=COLORS[name],label=name)
        style(ax)
        ax.axhline(0,color="#4a515b",linewidth=.7)
        ax.set_xticks(range(len(years)),[f"{y} H2" if y==2016 else str(y) for y in years])
        ax.set_title(title)
        ax.yaxis.set_major_formatter(PercentFormatter(1))
    axes[0].set_ylabel("Calendar-year return")
    fig.legend(*axes[0].get_legend_handles_labels(),loc="upper center",bbox_to_anchor=(.5,.93),ncol=3,frameon=False)
    fig.suptitle("Year-by-year returns — frozen strategy and references",y=.99,fontsize=14)
    fig.text(.06,.025,"2016 is partial; later years link prior year-end close | Cash-start portfolios vs price-index reference | No dividends",fontsize=8,color="#404852")
    fig.subplots_adjust(top=.80,bottom=.15,left=.07,right=.98,wspace=.10)
    verify_labels(fig)
    fig.savefig(DESTINATION/"yearly_returns.png",facecolor="white",metadata={"Description":"Derived portfolio and reference calendar-year returns"})
    plt.close(fig)


def main():
    DESTINATION.mkdir(parents=True,exist_ok=True)
    design = curves(read(DESIGN/"resume_W-532.json"),read(DESIGN/"resume_eligible_equal_weight.json"))
    validation = curves(read(VALIDATION/"W-532_resume.json"),read(VALIDATION/"eligible_equal_weight_resume.json"))
    plt.rcParams.update({"font.family":"DejaVu Sans","font.size":10})
    equity_figure(design,"Design comparison | Jul 2016–Dec 2020 | halt=resume","design_equity.png")
    equity_figure(validation,"One-time validation: FAIL | Jan 2021–Dec 2024 | halt=resume","validation_equity.png")
    yearly_figure(design,validation)


if __name__ == "__main__":
    main()
