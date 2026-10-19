"""
data-service: FastAPI — Tercer microservicio del laboratorio integrador
Catálogo de productos sobre GCP Cloud SQL (PostgreSQL, IP privada).
Último salto de la cadena service-a → service-b → data-service.

Instrumentado con OTel DB Semantic Conventions estables:
  db.system.name, db.namespace, db.collection.name, db.operation.name,
  db.query.text, db.query.summary, server.address, server.port,
  db.response.returned_rows, error.type
"""

import logging
import os
import random
import time
from contextlib import asynccontextmanager, closing, contextmanager

import psycopg2
from psycopg2.extensions import parse_dsn
from fastapi import FastAPI, HTTPException
from pythonjsonlogger import jsonlogger

# ── OTel SDK ──────────────────────────────────────────────────────
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# ── Config ─────────────────────────────────────────────────────────
OTEL_ENDPOINT   = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
CLOUD_PROVIDER  = os.getenv("CLOUD_PROVIDER", "gcp")
APP_VERSION     = os.getenv("APP_VERSION", "1.0.0")
ENV             = os.getenv("ENVIRONMENT", "production")

# Cloud SQL con IP privada: postgresql://app:<pass>@10.x.x.x:5432/appdb
DB_DSN = os.getenv("DATABASE_URL", "postgresql://app:secret@postgres:5432/appdb")
_dsn   = parse_dsn(DB_DSN)
DB_HOST = _dsn.get("host", "unknown")
DB_PORT = int(_dsn.get("port", 5432))
DB_NAME = _dsn.get("dbname", "appdb")

# ── OTel Resource ──────────────────────────────────────────────────
resource = Resource.create({
    SERVICE_NAME:    "data-service",
    SERVICE_VERSION: APP_VERSION,
    "deployment.environment": ENV,
    "cloud.provider": CLOUD_PROVIDER,
})

# ── TracerProvider ─────────────────────────────────────────────────
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("data-service", APP_VERSION)

# ── MeterProvider ──────────────────────────────────────────────────
meter_provider = MeterProvider(
    resource=resource,
    metric_readers=[
        PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=OTEL_ENDPOINT, insecure=True),
            export_interval_millis=15000,
        )
    ],
)
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("data-service", APP_VERSION)

# ── Instrumentos de métricas ───────────────────────────────────────
# Nombre y unidad según la semconv de métricas de DB cliente.
# En Prometheus aparece como db_client_operation_duration_seconds.
db_operation_duration = meter.create_histogram(
    "db.client.operation.duration",
    description="Duración de operaciones contra Cloud SQL",
    unit="s",
)
db_errors_total = meter.create_counter(
    "db_errors_total",
    description="Total errores de base de datos",
    unit="1",
)

# ── Logging estructurado ───────────────────────────────────────────
class OtelJsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        span = trace.get_current_span()
        ctx  = span.get_span_context()
        if ctx and ctx.is_valid:
            log_record["trace_id"] = format(ctx.trace_id, "032x")
            log_record["span_id"]  = format(ctx.span_id, "016x")
        log_record["service"]        = "data-service"
        log_record["cloud_provider"] = CLOUD_PROVIDER

handler = logging.StreamHandler()
handler.setFormatter(OtelJsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("data-service")

# ── Auto-instrumentación ───────────────────────────────────────────
# Solo FastAPI. Psycopg2Instrumentor NO se usa aquí: emite los atributos
# legacy (db.statement, db.name) y duplicaría el span CLIENT manual, que
# sí sigue la semconv estable.
FastAPIInstrumentor().instrument(tracer_provider=tracer_provider)


def current_trace_id() -> str:
    return format(trace.get_current_span().get_span_context().trace_id, "032x")


@contextmanager
def db_span(operation: str, collection: str, query_text: str):
    """
    Span CLIENT + métrica de duración para una operación de DB.
    Nombre del span = db.query.summary ("SELECT products"), como pide la semconv.
    """
    summary = f"{operation} {collection}"
    attrs = {
        "db.system.name":     "postgresql",
        "db.namespace":       DB_NAME,
        "db.collection.name": collection,
        "db.operation.name":  operation,
        "db.query.summary":   summary,
        # Query parametrizada (%s): nunca lleva valores del usuario → sin PII
        "db.query.text":      query_text,
        "server.address":     DB_HOST,
        "server.port":        DB_PORT,
    }
    metric_attrs = {k: attrs[k] for k in
                    ("db.system.name", "db.namespace", "db.collection.name", "db.operation.name")}
    start = time.perf_counter()
    with tracer.start_as_current_span(summary, kind=trace.SpanKind.CLIENT, attributes=attrs) as span:
        try:
            yield span
        except psycopg2.Error as e:
            error_type = e.pgcode or type(e).__name__
            span.set_attribute("error.type", error_type)
            if e.pgcode:
                span.set_attribute("db.response.status_code", e.pgcode)
            span.record_exception(e)
            span.set_status(trace.StatusCode.ERROR, str(e))
            metric_attrs["error.type"] = error_type
            db_errors_total.add(1, {"db.operation.name": operation})
            raise
        finally:
            db_operation_duration.record(time.perf_counter() - start, metric_attrs)


# ── Chaos flags (fallback del Módulo D) ────────────────────────────
# El experimento oficial usa Chaos Mesh (fault externo a la app).
# Estas variables quedan solo como plan B si HTTPChaos no funciona.
CHAOS_LATENCY_MS  = int(os.getenv("CHAOS_LATENCY_MS", "0"))
CHAOS_ERROR_RATE  = float(os.getenv("CHAOS_ERROR_RATE", "0"))

def apply_chaos():
    if CHAOS_LATENCY_MS > 0:
        time.sleep(CHAOS_LATENCY_MS / 1000)
        logger.warning("Chaos: latencia inyectada", extra={"latency_ms": CHAOS_LATENCY_MS})

    if CHAOS_ERROR_RATE > 0 and random.random() < CHAOS_ERROR_RATE:
        logger.error("Chaos: error inyectado", extra={"error_rate": CHAOS_ERROR_RATE})
        raise HTTPException(status_code=500, detail="Chaos: error inyectado deliberadamente")


# ── App ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("data-service iniciando", extra={"db_host": DB_HOST, "db_name": DB_NAME})
    yield
    tracer_provider.shutdown()
    meter_provider.shutdown()

app = FastAPI(
    title="Data Service",
    description="Catálogo de productos sobre Cloud SQL con OTel DB Semantic Conventions",
    version=APP_VERSION,
    lifespan=lifespan,
)

# Los endpoints son `def` (no `async def`) a propósito: psycopg2 y time.sleep
# son bloqueantes. Se usa closing() porque `with psycopg2.connect()` solo
# cierra la transacción, no la conexión. FastAPI ejecuta los `def` en su threadpool, así un request
# lento no congela el event loop ni contamina la latencia de los demás.


@app.get("/health")
def health():
    return {"status": "ok", "service": "data-service", "cloud": CLOUD_PROVIDER}


@app.get("/data/products/{sku}")
def get_product(sku: str):
    """Detalle de un producto por SKU — lo llama service-b en cada request."""
    apply_chaos()

    query = "SELECT sku, name, category, price FROM products WHERE sku = %s"
    try:
        with db_span("SELECT", "products", query) as span:
            with closing(psycopg2.connect(DB_DSN)) as conn, conn.cursor() as cur:
                cur.execute(query, (sku,))
                row = cur.fetchone()
            span.set_attribute("db.response.returned_rows", 1 if row else 0)
    except psycopg2.Error as e:
        logger.error("DB query fallida", extra={"error": str(e), "sku": sku})
        raise HTTPException(status_code=500, detail="Database error")

    if not row:
        raise HTTPException(status_code=404, detail=f"Product {sku} not found")

    logger.info("Producto consultado", extra={"sku": sku})
    return {
        "sku": row[0], "name": row[1], "category": row[2], "price": float(row[3]),
        "trace_id": current_trace_id(),
    }


@app.get("/data/products")
def list_products(category: str = "all"):
    """Catálogo filtrado por categoría."""
    apply_chaos()

    query = "SELECT sku, name, category, price FROM products"
    params: list = []
    if category != "all":
        query += " WHERE category = %s"
        params.append(category)
    query += " LIMIT 50"

    try:
        with db_span("SELECT", "products", query) as span:
            with closing(psycopg2.connect(DB_DSN)) as conn, conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
            span.set_attribute("db.response.returned_rows", len(rows))
    except psycopg2.Error as e:
        logger.error("DB query fallida", extra={"error": str(e), "category": category})
        raise HTTPException(status_code=500, detail="Database error")

    logger.info("Catálogo consultado", extra={"rows": len(rows), "category": category})
    return {
        "products": [{"sku": r[0], "name": r[1], "category": r[2], "price": float(r[3])} for r in rows],
        "count":    len(rows),
        "trace_id": current_trace_id(),
    }


@app.get("/chaos/status")
def chaos_status():
    """Estado de los flags de caos de fallback."""
    return {
        "chaos_latency_ms": CHAOS_LATENCY_MS,
        "chaos_error_rate": CHAOS_ERROR_RATE,
        "chaos_active":     CHAOS_LATENCY_MS > 0 or CHAOS_ERROR_RATE > 0,
    }
