"""Vẽ biểu đồ cho báo cáo (PNG, nền sáng, in được). Màu theo design cố định, không xoay vòng."""

from __future__ import annotations

import os
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

COLORS = {"react": "#2a78d6", "pe": "#eb6834", "hybrid": "#1baf7a"}  # slot 1-3 bảng màu đã kiểm CVD
LABEL = {"react": "ReAct", "pe": "Plan-then-Execute", "hybrid": "Lai"}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e6e5e0"
SCEN = ["co-ban", "khu-hoi", "het-cho", "timeout", "can-duyet", "khong-co-ngay"]


def _style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def grouped_bar(rows: list[dict[str, Any]], metric: str, title: str, ylabel: str, path: str, *,
                harness: bool = True, noisy: bool = True, pct: bool = False) -> None:
    fig, ax = plt.subplots(figsize=(8.2, 3.6), dpi=160)
    width = 0.26
    for j, d in enumerate(("react", "pe", "hybrid")):
        vals = []
        for sid in SCEN:
            r = next(x for x in rows if x["scenario"] == sid and x["design"] == d and x["harness"] == harness and x["noisy"] == noisy)
            vals.append(r[metric] * (100 if pct else 1))
        xs = [i + (j - 1) * (width + 0.02) for i in range(len(SCEN))]
        bars = ax.bar(xs, vals, width=width, color=COLORS[d], label=LABEL[d], edgecolor="white", linewidth=1.2)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height(), f"{v:.0f}" if (pct or v >= 100) else f"{v:.1f}",
                    ha="center", va="bottom", fontsize=7, color=MUTED)
    ax.set_xticks(range(len(SCEN)), SCEN)
    ax.set_ylabel(ylabel, color=MUTED, fontsize=9)
    ax.set_title(title, loc="left", color=INK, fontsize=11, fontweight="bold")
    if pct:
        ax.set_ylim(0, 112)
    _style(ax)
    ax.legend(frameon=False, fontsize=8, ncol=3, loc="upper right", bbox_to_anchor=(1, 1.16))
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def token_growth(series: dict[str, list[int]], path: str, title: str, labels: dict[str, str] | None = None) -> None:
    """Token đầu vào của từng lần gọi model, theo thứ tự lần gọi (chi phí của lịch sử)."""
    fig, ax = plt.subplots(figsize=(8.2, 3.4), dpi=160)
    for d, ys in series.items():
        xs = list(range(1, len(ys) + 1))
        lab = (labels or {}).get(d, LABEL[d])
        ax.plot(xs, ys, color=COLORS[d], linewidth=2, marker="o", markersize=4, label=lab)
        ax.annotate(f"tổng {sum(ys):,}".replace(",", "."), (xs[-1], ys[-1]), textcoords="offset points",
                    xytext=(6, 0), fontsize=8, color=INK, va="center")
    ax.set_xlabel("lần gọi model thứ", color=MUTED, fontsize=9)
    ax.set_ylabel("token đầu vào / lần gọi", color=MUTED, fontsize=9)
    ax.set_title(title, loc="left", color=INK, fontsize=11, fontweight="bold")
    ax.set_xlim(0.5, max(len(v) for v in series.values()) + 2.2)
    _style(ax)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def noise_curve(curve: dict[str, list[tuple[float, float]]], path: str, title: str) -> None:
    fig, ax = plt.subplots(figsize=(8.2, 3.4), dpi=160)
    for d, pts in curve.items():
        xs = [p * 100 for p, _ in pts]
        ys = [v * 100 for _, v in pts]
        ax.plot(xs, ys, color=COLORS[d], linewidth=2, marker="o", markersize=5, label=LABEL[d])
        ax.annotate(f"{ys[-1]:.0f}%", (xs[-1], ys[-1]), textcoords="offset points", xytext=(6, 0), fontsize=8, color=INK, va="center")
    ax.set_xlabel("tỉ lệ nhiễu của model (% quyết định bị lỗi)", color=MUTED, fontsize=9)
    ax.set_ylabel("tỉ lệ đạt (%)", color=MUTED, fontsize=9)
    ax.set_ylim(0, 105)
    ax.set_title(title, loc="left", color=INK, fontsize=11, fontweight="bold")
    _style(ax)
    ax.legend(frameon=False, fontsize=8, loc="lower left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path
