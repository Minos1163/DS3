#!/usr/bin/env python3
"""批量运行不同 RR 的回测并生成对比表与图表。

用法：直接运行本脚本。脚本会：
 - 修改 `config/trading_config.json` 中的 `dca_rotation.params.rr_ratio`
 - 顺序运行 `backtest_dca_rotation.py` 对每个 RR 进行回测
 - 收集回测终端输出，生成 CSV 汇总并绘图保存到 `logs/`
 - 运行结束后恢复原始配置
"""

import re
import csv
import json
import os
import sys
import subprocess
from datetime import datetime


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def run_backtest(python_exe, script_path):
    proc = subprocess.run([python_exe, script_path], capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    return out


def parse_output(out):
    # 提取关键信息
    res = {}
    m = re.search(r"初始资金:\s*([\d\.\-]+)\s*USDT", out)
    if m:
        res["start"] = float(m.group(1))
    m = re.search(r"最终资金:\s*([\d\.\-]+)\s*USDT", out)
    if m:
        res["final"] = float(m.group(1))
    m = re.search(r"总收益:\s*([\+\-]?[\d\.]+)\s*USDT\s*\(([\+\-]?[\d\.]+)%\)", out)
    if m:
        res["profit"] = float(m.group(1))
        res["profit_pct"] = float(m.group(2))
    m = re.search(r"最大回撤:\s*([\d\.]+)%", out)
    if m:
        res["max_dd_pct"] = float(m.group(1))
    m = re.search(r"总交易:\s*(\d+)", out)
    if m:
        res["trades"] = int(m.group(1))
    m = re.search(r"胜率:\s*([\d\.]+)%", out)
    if m:
        res["winrate_pct"] = float(m.group(1))

    # 日志文件路径
    m = re.search(r"✅\s*交易记录:\s*(.+)", out)
    if m:
        res["trade_log"] = m.group(1).strip()
    m = re.search(r"✅\s*候选记录:\s*(.+)", out)
    if m:
        res["candidate_log"] = m.group(1).strip()
    m = re.search(r"✅\s*摘要:\s*(.+)", out)
    if m:
        res["summary_log"] = m.group(1).strip()

    return res


def ensure_logs_dir(root):
    logs = os.path.join(root, "logs")
    os.makedirs(logs, exist_ok=True)
    return logs


def main():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    config_path = os.path.join(repo_root, "config", "trading_config.json")
    backtest_script = os.path.join(repo_root, "backtest_dca_rotation.py")
    python_exe = sys.executable

    if not os.path.exists(config_path):
        print("找不到配置文件:", config_path)
        sys.exit(1)

    cfg = load_config(config_path)
    # 路径校验
    logs_dir = ensure_logs_dir(repo_root)

    # 记录并备份原始 rr
    orig_rr = None
    try:
        orig_rr = cfg.get("dca_rotation", {}).get("params", {}).get("rr_ratio")
    except Exception:
        orig_rr = None

    rrs = [1.0, 1.2, 1.5]
    results = []

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        for rr in rrs:
            print("\n==== 运行 RR =", rr, "====")
            # 修改配置
            if "dca_rotation" not in cfg:
                cfg["dca_rotation"] = {}
            if "params" not in cfg["dca_rotation"]:
                cfg["dca_rotation"]["params"] = {}
            cfg["dca_rotation"]["params"]["rr_ratio"] = float(rr)
            # 保证 rr_force 为 true（可选）
            cfg["dca_rotation"]["params"]["rr_force"] = cfg["dca_rotation"]["params"].get("rr_force", True)
            save_config(config_path, cfg)

            # 运行回测
            out = run_backtest(python_exe, backtest_script)

            # 保存原始输出
            out_file = os.path.join(logs_dir, f"rr_run_{rr}_{timestamp}.txt")
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(out)

            parsed = parse_output(out)
            parsed["rr"] = rr
            parsed["stdout_path"] = out_file
            results.append(parsed)

        # 生成 CSV 汇总
        csv_path = os.path.join(logs_dir, f"rr_comparison_summary_{timestamp}.csv")
        keys = [
            "rr",
            "final",
            "profit",
            "profit_pct",
            "max_dd_pct",
            "trades",
            "winrate_pct",
            "trade_log",
            "summary_log",
            "stdout_path",
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as cf:
            writer = csv.DictWriter(cf, fieldnames=keys)
            writer.writeheader()
            for r in results:
                row = {k: r.get(k, "") for k in keys}
                writer.writerow(row)

        # 绘图（需要 matplotlib）
        try:
            import matplotlib.pyplot as plt

            r_vals = [r["rr"] for r in results]
            equities = [r.get("final", None) for r in results]
            _trades = [r.get("trades", None) for r in results]
            winrates = [r.get("winrate_pct", None) for r in results]

            fig, ax1 = plt.subplots(figsize=(8, 4))
            ax1.plot(r_vals, equities, marker="o", label="Final Equity (USDT)")
            ax1.set_xlabel("RR")
            ax1.set_ylabel("Final Equity (USDT)")
            ax1.grid(True)

            ax2 = ax1.twinx()
            ax2.plot(r_vals, winrates, marker="x", color="orange", label="Winrate %")
            ax2.set_ylabel("Winrate %")

            lines, labels = ax1.get_legend_handles_labels()
            lines2, labels2 = ax2.get_legend_handles_labels()
            ax1.legend(lines + lines2, labels + labels2, loc="best")

            plt.title("RR 比较：Final Equity & Winrate")
            plot_path = os.path.join(logs_dir, f"rr_comparison_plot_{timestamp}.png")
            plt.tight_layout()
            plt.savefig(plot_path)
            plt.close(fig)
        except Exception as e:
            plot_path = None
            print("绘图失败（可能缺少 matplotlib）:", e)

        print("\n完成。CSV:", csv_path)
        if plot_path:
            print("图表:", plot_path)

    finally:
        # 恢复原始配置
        if orig_rr is not None:
            try:
                cfg = load_config(config_path)
                cfg["dca_rotation"]["params"]["rr_ratio"] = orig_rr
                save_config(config_path, cfg)
                print("已恢复原始 rr_ratio =", orig_rr)
            except Exception as e:
                print("恢复原始配置失败:", e)


if __name__ == "__main__":
    main()
