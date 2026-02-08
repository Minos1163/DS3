#!/usr/bin/env python3
"""Generate a self-contained HTML report (and attempt PDF) from experiment artifacts.

Produces: reports/report_full_<ts>.html and reports/report_full_<ts>.pdf (if wkhtmltopdf available)
"""
import os
import shutil
import subprocess
import base64
from datetime import datetime
import pandas as pd


def read_markdown(md_path):
    if not os.path.exists(md_path):
        return ""
    with open(md_path, "r", encoding="utf-8") as f:
        return f.read()


def embed_image(path):
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        b = f.read()
    mime = "image/png"
    data = base64.b64encode(b).decode("ascii")
    return f"data:{mime};base64,{data}"


def csv_to_html_table(csv_path, max_rows=200):
    if not os.path.exists(csv_path):
        return f"<p>Missing CSV: {csv_path}</p>"
    df = pd.read_csv(csv_path)
    return df.head(max_rows).to_html(index=False, classes="table table-sm", border=0)


def collect_plots(plots_dir="reports/plots"):
    out = []
    if not os.path.exists(plots_dir):
        return out
    for fn in sorted(os.listdir(plots_dir)):
        if fn.lower().endswith(".png"):
            out.append(os.path.join(plots_dir, fn))
    return out


def generate_html(report_md, top_csv, cross_csv, btc_csv, plots, out_html):
    title = "实验完整报告"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    parts = []
    parts.append(f'<html><head><meta charset="utf-8"><title>{title}</title>')
    parts.append(
        '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">'
    )
    parts.append('</head><body class="p-3">')
    parts.append(f"<h1>{title}</h1>")
    parts.append(f"<p>生成时间: {now}</p>")

    parts.append("<h2>实验摘要</h2>")
    parts.append('<pre style="white-space:pre-wrap;">')
    parts.append(report_md)
    parts.append("</pre>")

    parts.append("<h2>Top-N 参数表（示例）</h2>")
    parts.append(csv_to_html_table(top_csv, max_rows=50))

    parts.append("<h2>Cross-sample 结果</h2>")
    parts.append(csv_to_html_table(cross_csv, max_rows=200))

    parts.append("<h2>BTC 验证结果</h2>")
    parts.append(csv_to_html_table(btc_csv, max_rows=200))

    parts.append("<h2>Top-N 股权曲线</h2>")
    for p in plots:
        data_uri = embed_image(p)
        if not data_uri:
            continue
        parts.append(f'<div style="margin-bottom:18px"><h4>{os.path.basename(p)}</h4>')
        parts.append(
            f'<img src="{data_uri}" style="max-width:100%;height:auto;border:1px solid #ccc;padding:6px;background:#fff"/>'
        )
        parts.append("</div>")

    parts.append("<hr/><p>报告由脚本自动生成。</p>")
    parts.append("</body></html>")

    html = "\n".join(parts)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    return out_html


def try_convert_pdf(html_path, pdf_path):
    # Try wkhtmltopdf
    wk = shutil.which("wkhtmltopd")
    if wk is None:
        print("wkhtmltopdf not found; skipping PDF conversion. You can install wkhtmltopdf and run:")
        print(f'  wkhtmltopdf "{html_path}" "{pdf_path}"')
        return False
    cmd = [wk, html_path, pdf_path]
    try:
        subprocess.check_call(cmd)
        return True
    except Exception as e:
        print("PDF conversion failed:", e)
        return False


def main():
    os.makedirs("reports", exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_html = f"reports/report_full_{ts}.html"
    out_pdf = f"reports/report_full_{ts}.pdf"

    report_md = read_markdown("reports/experiment_summary.md")
    top_csv_candidates = sorted([p for p in os.listdir("logs") if p.startswith("deep_grid_parallel_top")])
    top_csv = os.path.join("logs", top_csv_candidates[-1]) if top_csv_candidates else ""
    cross_csv = ""
    # pick latest cross_top or cross_validation
    for name in ["cross_top10_results_20260202_152233.csv", "cross_top10_results_20260202_152025.csv"]:
        if os.path.exists(os.path.join("logs", name)):
            cross_csv = os.path.join("logs", name)
            break
    if not cross_csv:
        # fallback
        cross_list = sorted(
            [p for p in os.listdir("logs") if p.startswith("cross_top") or p.startswith("cross_validation")]
        )
        cross_csv = os.path.join("logs", cross_list[-1]) if cross_list else ""

    btc_csv = ""
    btc_list = sorted([p for p in os.listdir("logs") if p.startswith("btc_top")])
    btc_csv = os.path.join("logs", btc_list[-1]) if btc_list else ""

    plots = collect_plots("reports/plots")
    print("Using top_csv=", top_csv, " cross_csv=", cross_csv, " btc_csv=", btc_csv)
    html = generate_html(report_md, top_csv, cross_csv, btc_csv, plots, out_html)
    print("HTML report generated:", html)
    ok = try_convert_pdf(html, out_pdf)
    if ok:
        print("PDF generated:", out_pdf)
    else:
        print("PDF not generated; HTML is available.")


if __name__ == "__main__":
    main()
