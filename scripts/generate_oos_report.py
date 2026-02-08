#!/usr/bin/env python3
import os

import pandas as pd

import matplotlib.pyplot as plt

import seaborn as sns

"""
生成 OOS 网格扫描报告（Markdown + 热力图）
输入: logs/oos_grid_scan_results.csv
输出:
 - docs/oos_grid_report.md
 - docs/oos_grid_heatmap.png
 - logs/pr_draft.md （追加）
"""

try:
    import seaborn as sns
except Exception:
    sns = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
logs = os.path.join(ROOT, "logs")
docs = os.path.join(ROOT, "docs")
os.makedirs(docs, exist_ok=True)

INPUT_CSV = os.path.join(logs, "oos_grid_scan_results.csv")
OUT_MD = os.path.join(docs, "oos_grid_report.md")
OUT_IMG = os.path.join(docs, "oos_grid_heatmap.png")
PR_DRAFT = os.path.join(logs, "pr_draft.md")

if not os.path.exists(INPUT_CSV):
    print(f"ERROR: 输入文件不存在: {INPUT_CSV}")
    raise SystemExit(1)

# 读取 CSV
df = pd.read_csv(INPUT_CSV)
#!/usr/bin/env python3
"""
生成 OOS 网格扫描报告（Markdown + 热力图）
输入: logs/oos_grid_scan_results.csv
输出:
 - docs/oos_grid_report.md
 - docs/oos_grid_heatmap.png
 - logs/pr_draft.md （追加）
"""

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
logs = os.path.join(ROOT, "logs")
docs = os.path.join(ROOT, "docs")
os.makedirs(docs, exist_ok=True)

INPUT_CSV = os.path.join(logs, "oos_grid_scan_results.csv")
OUT_MD = os.path.join(docs, "oos_grid_report.md")
OUT_IMG = os.path.join(docs, "oos_grid_heatmap.png")
PR_DRAFT = os.path.join(logs, "pr_draft.md")

if not os.path.exists(INPUT_CSV):
    print(f"ERROR: 输入文件不存在: {INPUT_CSV}")
    raise SystemExit(1)

# 读取 CSV
df = pd.read_csv(INPUT_CSV)
# 标准化列名（小写，去空格）
df.columns = [c.strip() for c in df.columns]
# 关键列名猜测与兼容
for col in ["stop_loss_pct", "stop_loss", "stop_loss_percent"]:
    if col in df.columns:
        stop_col = col
        break
else:
    stop_col = None

for col in ["position_percent", "position", "position_pct"]:
    if col in df.columns:
        pos_col = col
        break
else:
    pos_col = None

# final capital 列
for col in ["final_capital", "final_cap", "capital"]:
    if col in df.columns:
        cap_col = col
        break
else:
    cap_col = None

# drawdown
dd_col = None
for col in ["max_drawdown_pct", "max_drawdown", "drawdown"]:
    if col in df.columns:
        dd_col = col
        break

if stop_col is None or pos_col is None or cap_col is None:
    print("ERROR: 无法识别 CSV 中的必要列:", df.columns.tolist())
    raise SystemExit(1)

# 确保数值列为 float
for c in [stop_col, pos_col, cap_col]:
    df[c] = pd.to_numeric(df[c], errors="coerce")

# Top-N
TOP_N = 10
# 排序依据 final capital（降序）
best_overall = df.sort_values(by=cap_col, ascending=False).head(TOP_N)

# per-file best: 按 file 或 symbol
key_file = None
for col in ["file", "filepath", "data_file"]:
    if col in df.columns:
        key_file = col
        break
if key_file is None and "symbol" in df.columns:
    key_file = "symbol"

if key_file is None:
    # fallback to index grouping
    df["_grp"] = range(len(df))
    key_file = "_grp"

best_per_file = df.loc[df.groupby(key_file)[cap_col].idxmax()].sort_values(by=cap_col, ascending=False)

# 生成热力图数据：对 stop x pos 聚合 final capital 的均值
heat = df.groupby([stop_col, pos_col])[cap_col].mean().reset_index()
heat_pivot = heat.pivot(index=stop_col, columns=pos_col, values=cap_col)
# 为显示从小到大排序
heat_pivot = heat_pivot.sort_index(ascending=True)
heat_pivot = heat_pivot[sorted(heat_pivot.columns)]

# 绘制热力图
plt.figure(figsize=(10, max(4, len(heat_pivot) * 0.5)))
if sns is not None:
    sns.set(style="whitegrid")
    ax = sns.heatmap(heat_pivot, annot=True, fmt=".1", cmap="viridis", cbar_kws={"label": "avg final capital"})
    ax.set_xlabel("position_percent")
    ax.set_ylabel("stop_loss_pct")
else:
    ax = plt.gca()
    im = ax.imshow(heat_pivot.values, aspect="auto", cmap="viridis", origin="lower")
    ax.set_xlabel("position_percent")
    ax.set_ylabel("stop_loss_pct")
    # 设置坐标标签
    ax.set_xticks(range(len(heat_pivot.columns)))
    ax.set_xticklabels([str(c) for c in heat_pivot.columns], rotation=45)
    ax.set_yticks(range(len(heat_pivot.index)))
    ax.set_yticklabels([str(i) for i in heat_pivot.index])
    plt.colorbar(im, label="avg final capital")

plt.title("OOS grid: avg final capital (stop_loss vs position)")
plt.tight_layout()
plt.savefig(OUT_IMG, dpi=150)
plt.close()

# 生成 Markdown
lines = []
lines.append("# OOS Grid Scan 报告\n")
lines.append(f"生成时间: {pd.Timestamp.now()}\n")
lines.append("## 概要\n")
lines.append(f"输入文件: `{INPUT_CSV}`\n")
lines.append(f"样本数: {len(df)} 条记录\n")
lines.append(f"Top-{TOP_N}（按 `{cap_col}` 排序）:\n")
lines.append("\n")
# Top table
lines.append("| Rank | file | symbol | stop_loss_pct | position_percent | final_capital | max_drawdown_pct |\n")
lines.append("|---:|---|---|---:|---:|---:|---:|\n")
for i, r in best_overall.iterrows():
    fileval = r.get(key_file, "")
    sym = r.get("symbol", "")
    dd = r.get(dd_col, "") if dd_col else ""
    lines.append(
        f"| {len(lines)} | `{fileval}` | {sym} | {r[stop_col]:.4f} | {r[pos_col]:.3f} | {r[cap_col]:.2f} | {dd} |\n"
    )

lines.append("\n## 每文件最优参数（按 final capital）\n")
lines.append("| file | symbol | stop_loss_pct | position_percent | final_capital | max_drawdown_pct |\n")
lines.append("|---|---|---:|---:|---:|---:|\n")
for i, r in best_per_file.iterrows():
    fileval = r.get(key_file, "")
    sym = r.get("symbol", "")
    dd = r.get(dd_col, "") if dd_col else ""
    lines.append(f"| `{fileval}` | {sym} | {r[stop_col]:.4f} | {r[pos_col]:.3f} | {r[cap_col]:.2f} | {dd} |\n")

lines.append("\n## 热力图（stop_loss vs position）\n")
rel = os.path.relpath(OUT_IMG, start=logs)
lines.append(f"![OOS heatmap]({rel})\n")
lines.append("\n---\n")

# 写入 docs/oos_grid_report.md
with open(OUT_MD, "w", encoding="utf-8") as f:
    f.writelines(lines)

# 追加到 PR 草案
pr_lines = ["\n\n## OOS Grid Scan 快速汇总（自动追加）\n\n"]
pr_lines += lines
with open(PR_DRAFT, "a", encoding="utf-8") as f:
    f.writelines(pr_lines)

print(f"WROTE: {OUT_MD}")
print(f"WROTE: {OUT_IMG}")
print(f"APPENDED: {PR_DRAFT}")
print("Done.")
