"""
Automated LaTeX Financial Digest & Scorecard Generator for Delilah OS.
Compiles executive weekly and monthly financial digests into high-quality LaTeX documents.
"""

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path


def latex_escape(text: str) -> str:
    """Safely escape text for LaTeX compilation."""
    if not text:
        return ""
    text = str(text)
    # Special character map
    conv = {
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
        "\\": r"\textbackslash{}",
    }
    regex = re.compile("|".join(re.escape(key) for key in sorted(conv.keys(), key=lambda item: -len(item))))
    return regex.sub(lambda match: conv[match.group()], text)


def build_latex_digest(digest: dict, user_name: str = "Delilah User") -> str:
    """
    Construct a complete LaTeX article representing the user's financial digest and scorecard.
    """
    period = digest.get("period_label", "Financial")
    gen_at = digest.get("generated_at", datetime.now().strftime("%Y-%m-%d"))
    score = digest.get("financial_health_score", 75)
    grade = digest.get("grade", "B")

    inflow = digest.get("inflow", 0.0)
    outflow = digest.get("outflow", 0.0)
    net_cf = digest.get("net_cash_flow", 0.0)
    savings_rate = digest.get("savings_rate_pct", 0.0)
    tx_count = digest.get("transaction_count", 0)

    debt_info = digest.get("debt_summary", {})
    total_debt = debt_info.get("total_debt", 0.0)
    debt_count = debt_info.get("debt_count", 0)

    sf_info = digest.get("sinking_funds_summary", {})
    sf_saved = sf_info.get("total_saved", 0.0)
    sf_target = sf_info.get("total_target", 0.0)
    sf_pct = sf_info.get("progress_pct", 0.0)

    trials_cnt = digest.get("active_trials_count", 0)

    # Health score color
    if score >= 80:
        score_color = "teal"
    elif score >= 65:
        score_color = "olive"
    else:
        score_color = "purple"

    # Category rows
    cat_rows = []
    for cat in digest.get("category_spending", [])[:8]:
        cat_name = latex_escape(cat.get("category", "Other"))
        amt = cat.get("amount", 0.0)
        pct = cat.get("percentage", 0.0)
        cnt = cat.get("count", 0)
        cat_rows.append(f"  {cat_name} & \\${amt:,.2f} & {pct:.1f}\\% & {cnt} \\\\")
    category_table_body = "\n".join(cat_rows) if cat_rows else "  None recorded & \\$0.00 & 0\\% & 0 \\\\"

    # Outlier / top transaction rows
    top_tx_rows = []
    for tx in digest.get("top_transactions", [])[:5]:
        d = latex_escape(tx.get("date", "")[:10])
        m = latex_escape(tx.get("merchant", "Merchant"))
        c = latex_escape(tx.get("category", "General"))
        a = tx.get("amount", 0.0)
        top_tx_rows.append(f"  {d} & {m} & {c} & \\${a:,.2f} \\\\")
    top_tx_table_body = "\n".join(top_tx_rows) if top_tx_rows else "  --- & None & None & \\$0.00 \\\\"

    safe_user_name = latex_escape(user_name)

    tex_code = f"""\\documentclass[11pt,a4paper]{{article}}
\\usepackage[utf8]{{inputenc}}
\\usepackage[margin=1in]{{geometry}}
\\usepackage{{amsmath,amssymb}}
\\usepackage{{booktabs}}
\\usepackage{{tabularx}}
\\usepackage{{xcolor}}
\\usepackage{{tcolorbox}}
\\usepackage{{hyperref}}

\\definecolor{{delilahblue}}{{RGB}}{{41, 128, 185}}
\\definecolor{{delilahdark}}{{RGB}}{{44, 62, 80}}
\\definecolor{{delilahgreen}}{{RGB}}{{39, 174, 96}}
\\definecolor{{delilahred}}{{RGB}}{{192, 57, 43}}

\\hypersetup{{
    colorlinks=true,
    linkcolor=delilahblue,
    urlcolor=delilahblue
}}

\\title{{\\textbf{{\\huge Delilah Financial OS}} \\\\ \\Large {period} Executive Briefing \\& Financial Scorecard}}
\\author{{Account: \\textbf{{{safe_user_name}}}}}
\\date{{Report Generated: {gen_at}}}

\\begin{{document}}

\\maketitle
\\thispagestyle{{empty}}

\\begin{{tcolorbox}}[colback=delilahdark!5!white,colframe=delilahdark,title=\\textbf{{\\large Financial Health Scorecard: Grade {grade} ({score}/100)}}]
\\begin{{tabularx}}{{\\linewidth}}{{X r X r}}
  \\textbf{{Net Cash Flow:}} & \\${net_cf:,.2f} & \\textbf{{Savings Rate:}} & {savings_rate:.1f}\\% \\\\
  \\textbf{{Total Inflows:}} & \\${inflow:,.2f} & \\textbf{{Total Outflows:}} & \\${outflow:,.2f} \\\\
  \\textbf{{Settled Purchases:}} & {tx_count} & \\textbf{{Active Trials:}} & {trials_cnt} \\\\
  \\textbf{{Total Tracked Debt:}} & \\${total_debt:,.2f} ({debt_count} loans) & \\textbf{{Sinking Funds:}} & \\${sf_saved:,.2f} / \\${sf_target:,.2f} ({sf_pct:.1f}\\%) \\\\
\\end{{tabularx}}
\\end{{tcolorbox}}

\\vspace{{0.5em}}

\\section*{{1. Cash Flow \\& Savings Velocity}}
During this {period.lower()} cycle, total recorded revenue amounted to \\textbf{{\\${inflow:,.2f}}} against settled operating expenditures of \\textbf{{\\${outflow:,.2f}}}.
\\begin{{itemize}}
  \\item \\textbf{{Net Cash Surplus / Deficit:}} \\${net_cf:,.2f}
  \\item \\textbf{{Effective Savings Ratio:}} {savings_rate:.1f}\\%
  \\item \\textbf{{Sinking Fund Reserve Status:}} \\${sf_saved:,.2f} funded across envelopes towards a \\${sf_target:,.2f} goal ({sf_pct:.1f}\\% complete).
\\end{{itemize}}

\\section*{{2. Expenditure Breakdown by Category}}
\\begin{{center}}
\\begin{{tabularx}}{{0.95\\linewidth}}{{X r r r}}
\\toprule
\\textbf{{Category}} & \\textbf{{Amount Spent}} & \\textbf{{\\% of Outflow}} & \\textbf{{Tx Count}} \\\\
\\midrule
{category_table_body}
\\bottomrule
\\end{{tabularx}}
\\end{{center}}

\\section*{{3. Significant Outliers \\& Largest Outflows}}
\\begin{{center}}
\\begin{{tabularx}}{{0.95\\linewidth}}{{l X X r}}
\\toprule
\\textbf{{Date}} & \\textbf{{Merchant}} & \\textbf{{Category}} & \\textbf{{Amount}} \\\\
\\midrule
{top_tx_table_body}
\\bottomrule
\\end{{tabularx}}
\\end{{center}}

\\section*{{4. Actionable Directives}}
\\begin{{itemize}}
  \\item \\textbf{{Reserve Sweeps:}} Execute \\texttt{{!sinking sweep}} to allocate loose transaction change directly towards long-term envelopes.
  \\item \\textbf{{Recurring Subscriptions:}} {trials_cnt} free trials are currently flagged. Run \\texttt{{!trials list}} to review cancellation windows.
  \\item \\textbf{{Debt Amortization:}} If carrying balance, simulate optimized payoff using \\texttt{{!debt payoff avalanche}}.
\\end{{itemize}}

\\vfill
\\begin{{center}}
\\footnotesize \\textit{{Generated deterministically by Delilah Financial OS • Privacy-First Decentralized Ledger Engine}}
\\end{{center}}

\\end{{document}}
"""
    return tex_code


def export_financial_digest(
    period: str = "monthly",
    days: int = None,
    *,
    user_id: str,
    user_name: str = "Delilah User",
    output_dir: str = "data/reports"
) -> dict:
    """
    Produce the LaTeX financial digest, write to disk, and attempt PDF compilation if pdflatex exists.
    """
    from src.db.queries import generate_financial_digest
    digest = generate_financial_digest(period=period, days=days, user_id=user_id)

    tex_content = build_latex_digest(digest, user_name=user_name)

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tex_filename = f"digest_{user_id}_{period}_{timestamp}.tex"
    tex_file = out_path / tex_filename

    with open(tex_file, "w", encoding="utf-8") as f:
        f.write(tex_content)

    pdf_file = None
    pdf_compiled = False

    # Check for pdflatex
    import shutil
    pdflatex_bin = shutil.which("pdflatex")
    if pdflatex_bin:
        try:
            res = subprocess.run(
                [pdflatex_bin, "-interaction=nonstopmode", f"-output-directory={out_path}", str(tex_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False
            )
            expected_pdf = out_path / f"digest_{user_id}_{period}_{timestamp}.pdf"
            if expected_pdf.exists():
                pdf_file = str(expected_pdf)
                pdf_compiled = True
        except Exception:
            pass

    return {
        "digest": digest,
        "tex_path": str(tex_file),
        "pdf_path": pdf_file,
        "pdf_compiled": pdf_compiled,
        "tex_content": tex_content,
    }
