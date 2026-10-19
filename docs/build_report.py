#!/usr/bin/env python3
"""Rellena docs/reporte.html con los resultados reales de run_chaos.py y lo imprime a PDF con Chrome headless.

    python3 docs/build_report.py
"""
import ast
import html
import re
import subprocess
from pathlib import Path

DOCS = Path(__file__).resolve().parent
CHAOS = DOCS / "evidencias/chaos"
RESULTS = "\n".join((CHAOS / f).read_text() for f in ("resultados-exp1.txt", "resultados-exp23.txt")
                     if (CHAOS / f).exists())
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def section(n: int) -> str:
    parts = re.split(r"══ ", RESULTS)
    for p in parts:
        if p.startswith(f"EXPERIMENTO {n}"):
            return p
    return ""


def exp_values(n: int) -> dict:
    s = section(n)
    mttd = re.search(r"MTTD = (\d+)s \((CUMPLE|NO CUMPLE)", s)
    budget = re.search(r"error budget consumido .*?: ([\d.]+%)", s)
    bad = re.search(r"5xx en el borde durante el experimento: (\d+)", s)
    p99 = re.search(r"p99 durante: service-a=(\d+|n/a)ms?", s)
    if mttd:
        cls = "ok" if mttd.group(2) == "CUMPLE" else "warn"
        mttd_txt = f'<span class="{cls}">{mttd.group(1)} s ({mttd.group(2).lower()} ≤ 120 s)</span>'
    else:
        mttd_txt = '<span class="warn">sin alerta</span>'
    slo = []
    if p99 and p99.group(1) != "n/a" and int(p99.group(1)) > 500:
        slo.append(f"latencia: sí (p99 borde {p99.group(1)} ms &gt; 500 ms)")
    if bad and int(bad.group(1)) > 0:
        slo.append(f"disponibilidad: sí ({bad.group(1)} requests 5xx)")
    return {
        "MTTD": mttd_txt,
        "SLO": "; ".join(slo) or "no",
        "BUDGET": budget.group(1) if budget else "n/a",
    }


def noise() -> tuple[str, str]:
    st = re.search(r"Estáticas\s+\((\d+)\): (\{.*\})", RESULTS)
    dy = re.search(r"Dinámicas/AIOps \((\d+)\): (\{.*\})", RESULTS)
    if not (st and dy):
        return "n/a", "<p>Sin datos.</p>"
    s_n, d_n = int(st.group(1)), int(dy.group(1))
    s_d, d_d = ast.literal_eval(st.group(2)), ast.literal_eval(dy.group(2))
    pages = d_d.get("AIOpsCorrelatedAnomaly", 0)
    rows = "".join(f"<tr><td>Estática</td><td>{k}</td><td>{v}</td></tr>" for k, v in s_d.items())
    rows += "".join(f"<tr><td>Dinámica / AIOps</td><td>{k}</td><td>{v}</td></tr>" for k, v in d_d.items())
    table = (f"<table><tr><th>Tipo</th><th>Regla</th><th>Episodios</th></tr>{rows}"
             f"<tr><th colspan='2'>Total de episodios estáticas vs. dinámicas</th><th>{s_n} vs. {d_n}</th></tr>"
             f"<tr><th colspan='2'>Notificaciones que despiertan a alguien (page)</th><th>{s_n} → {pages}</th></tr></table>"
             f"<p><b>Lectura honesta:</b> en número bruto de episodios no hubo reducción ({s_n} vs. {d_n}) en esta corrida corta. "
             f"La reducción está en el <b>ruteo</b>: con solo umbrales estáticos cada episodio es una notificación ({s_n}); "
             f"en el esquema AIOps únicamente la regla correlacionada pagina ({pages}), mientras que 2σ y SLO de latencia son "
             "alertas de ticket. Además <code>Static_AnyError</code> disparó 7 episodios por errores aislados que no "
             "constituían incidente; la regla 2σ los ignoró hasta que la desviación fue significativa.</p>")
    return f"{s_n} → {pages} notificaciones de page", table


page = (DOCS / "reporte.html").read_text()
for n in (1, 2, 3):
    for k, v in exp_values(n).items():
        page = page.replace(f"{{{{EXP{n}_{k}}}}}", v)
summary, table = noise()
page = page.replace("{{NOISE}}", summary).replace("{{NOISE_TABLE}}", table)
page = page.replace("{{CHAOS_LOG}}", html.escape(RESULTS.strip()))

out_html = DOCS / "reporte_final.html"
out_html.write_text(page)
out_pdf = DOCS / "Reporte_Lab_Integrador.pdf"
subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                f"--print-to-pdf={out_pdf}", out_html.as_uri()], check=True, capture_output=True)
print(f"PDF: {out_pdf}")
