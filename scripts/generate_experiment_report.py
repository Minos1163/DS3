import os
import pandas as pd
from datetime import datetime

LOG_DIR = 'logs'
OUT_DIR = 'reports'
os.makedirs(OUT_DIR, exist_ok=True)

# files to summarize
files = {
    'tp_grid': 'logs/tp_grid_results_20260202_222026.csv',
    'abc_grid': 'logs/abc_grid_results_20260202_222330.csv',
    'quick_grid': 'logs/deep_grid_quick_20260202_230132.csv',
    'parallel_grid': None,
    'grid_results': 'logs/grid_results_20260202_221714.csv',
    'cross_validation': 'logs/cross_validation_20260202_222548.csv'
}

# find parallel grid file (latest matching prefix)
parallel_candidates = [f for f in os.listdir(LOG_DIR) if f.startswith('deep_grid_parallel_full_')]
parallel_candidates.sort()
if parallel_candidates:
    files['parallel_grid'] = os.path.join(LOG_DIR, parallel_candidates[-1])

summary_lines = []
summary_lines.append(f"# 实验汇总报告\n")
summary_lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

SKILLS_FILE = 'skills.txt'

# helper to read csv if exists
def read_csv_safe(path):
    if path and os.path.exists(path):
        try:
            return pd.read_csv(path)
        except Exception as e:
            return None
    return None

# Summarize function
for name, path in files.items():
    df = read_csv_safe(path)
    summary_lines.append(f"## {name} ({path if path else '未找到'})\n")
    if df is None:
        summary_lines.append(f"未找到或无法读取文件: {path}\n")
        continue
    summary_lines.append(f"行数: {len(df)}\n")
    # pick top performers by final_capital
    if 'final_capital' in df.columns:
        df['final_capital_num'] = pd.to_numeric(df['final_capital'], errors='coerce')
        best = df.sort_values('final_capital_num', ascending=False).head(3)
        summary_lines.append("Top 3 (by final capital):\n")
        for _, r in best.iterrows():
            summary_lines.append(f"- {r.to_dict()}\n")
    else:
        # for simple cross_validation format
        if 'final_capital' in df.columns or 'final_capital' in df.columns:
            pass
        else:
            # show basic stats if possible
            cols = df.columns.tolist()
            summary_lines.append(f"Columns: {cols}\n")
            # if cross validation, print rows
            if 'data_file' in df.columns:
                for _, r in df.iterrows():
                    summary_lines.append(f"- {r.to_dict()}\n")
    summary_lines.append('\n')

# Extract selected best from parallel grid if exists
if files.get('parallel_grid'):
    dfp = read_csv_safe(files['parallel_grid'])
    if dfp is not None and 'final_capital' in dfp.columns:
        dfp['final_capital_num'] = pd.to_numeric(dfp['final_capital'], errors='coerce')
        best = dfp.sort_values('final_capital_num', ascending=False).head(5)
        summary_lines.append('## Parallel grid top 5\n')
        for _, r in best.iterrows():
            summary_lines.append(f"- {r.to_dict()}\n")

# Add recommended parameters based on previous interactive choice (B3)
summary_lines.append('## 推荐参数（B3 版本）\n')
summary_lines.append("- position_percent: 0.30\n")
summary_lines.append("- leverage: 10\n")
summary_lines.append("- stop_loss: 0.006 (0.6%)\n")
summary_lines.append("- take_profit: 0.14 (14%)\n")
summary_lines.append("- volume_quantile: 0.30 (long) / 0.45 (short)\n")
summary_lines.append("- trailing_start: 0.03 | trailing_stop: 0.04\n")
summary_lines.append("- max_hold_bars: 60 | cooldown_bars: 12\n")

# Add SKILLS section from skills.txt
summary_lines.append('## SKILLS（编程AI & 交易AI 能力提升）\n')
if os.path.exists(SKILLS_FILE):
    with open(SKILLS_FILE, 'r', encoding='utf-8') as f:
        summary_lines.append(f.read().strip() + '\n')
else:
    summary_lines.append(f"未找到技能文件: {SKILLS_FILE}\n")

out_path = os.path.join(OUT_DIR, 'experiment_summary.md')
with open(out_path, 'w', encoding='utf-8') as f:
    f.writelines([l + '\n' for l in summary_lines])

print(f"Report written to {out_path}")
