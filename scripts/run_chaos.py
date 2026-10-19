"""
run_chaos.py — Orquesta los experimentos de caos (Módulo D) y mide:
  * MTTD: inicio del fault → alerta en estado FIRING en Prometheus
  * Enriquecimiento AIOps: trace_id de un request fallido/lento desde Jaeger
  * Consumo de error budget del SLO de disponibilidad del borde (service-a)
  * Ruido: episodios de alerta de reglas estáticas vs. dinámicas (AIOps)

Requiere port-forward: Prometheus en :9092 y Jaeger en :16686.
Uso: python3 scripts/run_chaos.py > docs/evidencias/chaos/resultados.txt
"""
import json
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROM = "http://localhost:9092"
JAEGER = "http://localhost:16686"
NS = "otel-lab"
SLO_AVAILABILITY = 0.995          # 99.5% en ventana de 30 días
RPS = 2                           # carga constante de k6 (db-f1-micro satura a 10 req/s)
BUDGET_30D = (1 - SLO_AVAILABILITY) * RPS * 86400 * 30   # requests "malos" permitidos

firing_episodes: dict[str, int] = {}
_prev_firing: set[str] = set()
_stop = False


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now()}] {msg}", flush=True)


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def prom_query(q: str) -> list:
    return get(f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": q}))["data"]["result"]


def firing() -> set[str]:
    alerts = get(f"{PROM}/api/v1/alerts")["data"]["alerts"]
    return {f"{a['labels']['alertname']}|{a['labels'].get('exported_job', '')}"
            for a in alerts if a["state"] == "firing"}


def watcher() -> None:
    """Cuenta episodios (transiciones a FIRING) de cada alerta durante toda la corrida."""
    global _prev_firing
    while not _stop:
        try:
            cur = firing()
            for key in cur - _prev_firing:
                name = key.split("|")[0]
                firing_episodes[name] = firing_episodes.get(name, 0) + 1
            _prev_firing = cur
        except Exception as e:  # noqa: BLE001 — el watcher no debe morir por un timeout
            log(f"watcher: {e}")
        time.sleep(5)


def wait_alert(names: set[str], t0: float, timeout: int = 300) -> tuple[str, float] | None:
    while time.time() - t0 < timeout:
        for key in firing():
            if key.split("|")[0] in names:
                return key, time.time() - t0
        time.sleep(3)
    return None


def enrich_trace_id(service: str, tags: dict) -> str:
    """Enriquecimiento AIOps: trace_id del request afectado más reciente."""
    q = urllib.parse.urlencode({"service": service, "limit": 1, "lookback": "5m",
                                "tags": json.dumps(tags)})
    try:
        data = get(f"{JAEGER}/api/traces?{q}")["data"]
    except OSError as e:
        return f"n/a (Jaeger no disponible: {e})"
    return data[0]["traceID"] if data else "n/a"


def bad_requests(window: str) -> float:
    r = prom_query(
        'sum(increase(otelcol_http_server_request_duration_seconds_count'
        f'{{exported_job="service-a",http_response_status_code=~"5.."}}[{window}]))')
    return float(r[0]["value"][1]) if r else 0.0


def p99(job: str) -> str:
    r = prom_query(f'svc:latency_p99:1m{{exported_job="{job}"}}')
    return f"{float(r[0]['value'][1]) * 1000:.0f}ms" if r else "n/a"


def sh(cmd: str) -> None:
    subprocess.run(cmd, shell=True, check=True, capture_output=True)


def experiment(title: str, start_cmd: str, stop_cmd: str, alerts: set[str],
               trace_service: str, trace_tags: dict, chaos_seconds: int) -> None:
    log(f"══ {title}")
    log(f"p99 antes: service-a={p99('service-a')} service-b={p99('service-b')}")
    t0 = time.time()
    sh(start_cmd)
    log("fault inyectado (T0)")
    hit = wait_alert(alerts, t0)
    if hit:
        key, mttd = hit
        verdict = "CUMPLE" if mttd <= 120 else "NO CUMPLE"
        log(f"ALERTA FIRING: {key}  →  MTTD = {mttd:.0f}s ({verdict} objetivo ≤ 120s)")
        log(f"Alerta enriquecida → trace_id: {enrich_trace_id(trace_service, trace_tags)}")
    else:
        log("SIN ALERTA dentro de 300s")
    remaining = chaos_seconds - (time.time() - t0)
    if remaining > 0:
        time.sleep(remaining)
    log(f"p99 durante: service-a={p99('service-a')} service-b={p99('service-b')}")
    elapsed = int(time.time() - t0)
    bad = bad_requests(f"{elapsed}s")
    log(f"requests 5xx en el borde durante el experimento: {bad:.0f}")
    log(f"error budget consumido (SLO {SLO_AVAILABILITY:.1%}/30d = {BUDGET_30D:,.0f} req): "
        f"{bad / BUDGET_30D:.3%}")
    sh(stop_cmd)
    log("fault revertido; esperando recuperación 60s")
    time.sleep(60)
    log(f"p99 después: service-a={p99('service-a')} service-b={p99('service-b')}")


EXP1_ON = "kubectl apply -f chaos-mesh/experiment-1-network-latency.yaml"
EXP1_OFF = "kubectl delete -f chaos-mesh/experiment-1-network-latency.yaml --ignore-not-found"
EXP2_ON = f"kubectl set env deployment/data-service -n {NS} CHAOS_ERROR_RATE=0.1"
EXP2_OFF = (f"kubectl set env deployment/data-service -n {NS} CHAOS_ERROR_RATE=0 && "
            f"kubectl rollout status deployment/data-service -n {NS} --timeout=120s")

if __name__ == "__main__":
    import os
    exps = set(os.getenv("EXPS", "1,2,3").split(","))
    threading.Thread(target=watcher, daemon=True).start()
    log("Warm-up de baseline: 90s con carga constante")
    time.sleep(90)

    if "1" in exps:
      experiment("EXPERIMENTO 1 — NetworkChaos 200ms en service-b",
               EXP1_ON, EXP1_OFF, {"LatencySLOBreach"},
               "service-a", {}, chaos_seconds=150)

    if "2" in exps:
      experiment("EXPERIMENTO 2 — error rate 10% en data-service",
               EXP2_ON, EXP2_OFF, {"ErrorRateAnomaly2Sigma"},
               "service-a", {"error": "true"}, chaos_seconds=150)

    if "3" in exps:
      experiment("EXPERIMENTO 3 — Combinado (correlación errores + latencia)",
               f"{EXP1_ON} && {EXP2_ON}", f"{EXP1_OFF} && {EXP2_OFF}",
               {"AIOpsCorrelatedAnomaly"}, "service-a", {"error": "true"}, chaos_seconds=150)

    _stop = True
    log("══ RUIDO: episodios de alerta durante toda la corrida")
    static = {k: v for k, v in firing_episodes.items() if k.startswith("Static_")}
    dynamic = {k: v for k, v in firing_episodes.items() if not k.startswith("Static_")}
    log(f"Estáticas  ({sum(static.values())}): {static}")
    log(f"Dinámicas/AIOps ({sum(dynamic.values())}): {dynamic}")
