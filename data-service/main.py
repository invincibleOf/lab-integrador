"""
data-service: FastAPI — Tercer microservicio del laboratorio integrador
Accede a GCP Cloud SQL (PostgreSQL) y AWS RDS según el cloud provider.
Instrumentado con OTel SDK completo siguiendo OTel DB Semantic Conventions.
"""

import logging
import os
import time
import random
import json
import psycopg2
from contextlib import asynccontextmanager
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
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor

# ── Config ─────────────────────────────────────────────────────────
OTEL_ENDPOINT   = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
CLOUD_PROVIDER  = os.getenv("CLOUD_PROVIDER", "gcp")
APP_VERSION     = os.getenv("APP_VERSION", "1.0.0")
ENV             = os.getenv("ENVIRONMENT", "production")

# DB según cloud provider
# GCP Cloud SQL: postgresql://user:pass@/dbname?host=/cloudsql/project:region:instance
# AWS RDS:       postgresql://user:pass@rds-endpoint:5432/dbname
DB_DSN = os.getenv("DATABASE_URL", "postgresql://app:secret@postgres:5432/appdb")

# ── OTel Resource ──────────────────────────────────────────────────
resource = Resource.create({
    SERVICE_NAME:    "data-service",
    SERVICE_VERSION: APP_VERSION,
    "deployment.environment": ENV,
    "cloud.provider": CLOUD_PROVIDER,
    # GCP Cloud SQL o AWS RDS según el provider
    "db.system": "postgresql",
    "db.provider": "cloud-sql" if CLOUD_PROVIDER == "gcp" else "rds",
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
db_query_duration = meter.create_histogram(
    "db_query_duration_seconds",
    description="Latencia de queries a Cloud SQL / RDS",
    unit="s",
)
db_connections_active = meter.create_up_down_counter(
    "db_connections_active",
    description="Conexiones activas a la base de datos",
    unit="1",
)
db_errors_total = meter.create_counter(
    "db_errors_total",
    description="Total errores de base de datos",
    unit="1",
)
# SLI: disponibilidad del servicio de datos
data_requests_total = meter.create_counter(
    "data_requests_total",
    description="Total requests al data-service",
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
        log_record["service"]       = "data-service"
        log_record["cloud_provider"] = CLOUD_PROVIDER

handler = logging.StreamHandler()
handler.setFormatter(OtelJsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("data-service")

# ── Auto-instrumentación ───────────────────────────────────────────
FastAPIInstrumentor().instrument(tracer_provider=tracer_provider)
Psycopg2Instrumentor().instrument(tracer_provider=tracer_provider)

# ── DB helpers ─────────────────────────────────────────────────────
def get_connection():
    """Conecta a Cloud SQL (GCP) o RDS (AWS) según CLOUD_PROVIDER."""
    return psycopg2.connect(DB_DSN)

# ── Chaos flags (Módulo D) ─────────────────────────────────────────
CHAOS_LATENCY_MS  = int(os.getenv("CHAOS_LATENCY_MS", "0"))    # inyectar latencia
CHAOS_ERROR_RATE  = float(os.getenv("CHAOS_ERROR_RATE", "0"))  # 0.0-1.0

def apply_chaos():
    """Aplica los experimentos de caos si están configurados."""
    if CHAOS_LATENCY_MS > 0:
        time.sleep(CHAOS_LATENCY_MS / 1000)
        logger.warning("Chaos: latencia inyectada", extra={"latency_ms": CHAOS_LATENCY_MS})

    if CHAOS_ERROR_RATE > 0 and random.random() < CHAOS_ERROR_RATE:
        logger.error("Chaos: error inyectado", extra={"error_rate": CHAOS_ERROR_RATE})
        raise HTTPException(status_code=500, detail="Chaos: error inyectado deliberadamente")

# ── App ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("data-service iniciando", extra={
        "cloud_provider": CLOUD_PROVIDER,
        "db_dsn_host": DB_DSN.split("@")[-1].split("/")[0],
    })
    yield
    tracer_provider.shutdown()
    meter_provider.shutdown()

app = FastAPI(
    title="Data Service",
    description="Tercer microservicio — acceso a Cloud SQL / RDS con OTel DB Semantic Conventions",
    version=APP_VERSION,
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "data-service", "cloud": CLOUD_PROVIDER}


@app.get("/data/products")
async def get_products(category: str = "all"):
    """
    Consulta catálogo de productos desde Cloud SQL (GCP) o RDS (AWS).
    Implementa OTel DB Semantic Conventions:
    - db.system: postgresql
    - db.operation: SELECT
    - db.sql.table: products
    - db.statement: (query completa)
    """
    data_requests_total.add(1, {"endpoint": "/data/products", "cloud": CLOUD_PROVIDER})

    # Aplicar chaos si está configurado (Módulo D)
    apply_chaos()

    start = time.time()
    db_connections_active.add(1)

    with tracer.start_as_current_span(
        "db.query.products",
        kind=trace.SpanKind.CLIENT,
        attributes={
            # ── OTel DB Semantic Conventions ─────────────────────
            "db.system":      "postgresql",
            "db.name":        "appdb",
            "db.operation":   "SELECT",
            "db.sql.table":   "products",
            "db.user":        "app",
            # Atributo del provider cloud
            "db.connection_string": f"{CLOUD_PROVIDER}-db",
            # Atributos de negocio
            "query.category": category,
            "query.cloud":    CLOUD_PROVIDER,
        }
    ) as span:
        try:
            conn = get_connection()
            cur  = conn.cursor()

            query = "SELECT id, name, category, price, stock FROM products"
            params = []
            if category != "all":
                query += " WHERE category = %s"
                params.append(category)
            query += " LIMIT 50"

            # Registrar el statement completo en el span
            span.set_attribute("db.statement", query)

            cur.execute(query, params)
            rows = cur.fetchall()
            conn.close()

            duration = time.time() - start
            db_query_duration.record(duration, {
                "operation":  "SELECT",
                "table":      "products",
                "cloud":      CLOUD_PROVIDER,
                "db.provider": "cloud-sql" if CLOUD_PROVIDER == "gcp" else "rds",
            })

            span.set_attribute("db.rows_returned", len(rows))
            span.set_attribute("db.query_duration_ms", round(duration * 1000, 2))
            span.set_status(trace.StatusCode.OK)

            logger.info("Products query completada", extra={
                "rows":     len(rows),
                "category": category,
                "duration": round(duration, 4),
                "cloud":    CLOUD_PROVIDER,
            })

            return {
                "products":      [{"id": r[0], "name": r[1], "category": r[2],
                                   "price": float(r[3]), "stock": r[4]} for r in rows],
                "count":         len(rows),
                "cloud_provider": CLOUD_PROVIDER,
                "db_provider":   "Cloud SQL" if CLOUD_PROVIDER == "gcp" else "RDS",
                "trace_id":      format(trace.get_current_span().get_span_context().trace_id, "032x"),
            }

        except HTTPException:
            raise
        except Exception as e:
            db_errors_total.add(1, {"operation": "SELECT", "cloud": CLOUD_PROVIDER})
            span.record_exception(e)
            span.set_status(trace.StatusCode.ERROR, str(e))
            logger.error("DB query fallida", extra={"error": str(e), "cloud": CLOUD_PROVIDER})
            raise HTTPException(status_code=500, detail=f"DB error: {str(e)}")
        finally:
            db_connections_active.add(-1)


@app.get("/data/analytics/{product_id}")
async def get_product_analytics(product_id: str):
    """
    Query analítica más costosa — para demostrar latencia en el flame graph.
    Simula un JOIN con tabla de ventas y cálculo de métricas agregadas.
    """
    apply_chaos()

    with tracer.start_as_current_span(
        "db.query.analytics",
        kind=trace.SpanKind.CLIENT,
        attributes={
            "db.system":     "postgresql",
            "db.operation":  "SELECT",
            "db.sql.table":  "sales",
            "db.statement":  "SELECT AVG(quantity), SUM(revenue) FROM sales WHERE product_id = ?",
            "product.id":    product_id,
            "query.type":    "analytics",
        }
    ) as span:
        # Simular latencia de query analítica (50-300ms)
        simulated_latency = random.uniform(0.05, 0.3)
        time.sleep(simulated_latency)

        span.set_attribute("db.query_duration_ms", round(simulated_latency * 1000, 2))
        span.set_attribute("analytics.simulated", True)

        result = {
            "product_id":     product_id,
            "avg_quantity":   round(random.uniform(1, 10), 2),
            "total_revenue":  round(random.uniform(1000, 50000), 2),
            "query_duration": round(simulated_latency * 1000, 2),
            "trace_id":       format(trace.get_current_span().get_span_context().trace_id, "032x"),
        }
        return result


@app.get("/chaos/status")
async def chaos_status():
    """Estado actual de los experimentos de caos (Módulo D)."""
    return {
        "chaos_latency_ms":  CHAOS_LATENCY_MS,
        "chaos_error_rate":  CHAOS_ERROR_RATE,
        "chaos_active":      CHAOS_LATENCY_MS > 0 or CHAOS_ERROR_RATE > 0,
    }
