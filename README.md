# Lab integrador — Observabilidad end-to-end con AIOps y resiliencia (GCP)

Sistema observable sobre **GKE + Cloud SQL** con tres microservicios encadenados
(`service-a → service-b → data-service`) instrumentados con **OpenTelemetry** (trazas, métricas
y logs correlacionados por `trace_id`), detección de anomalías **baseline + 2σ** con regla de
correlación, observabilidad de red (VPC Flow Logs, Dataplane V2) y **experimentos de caos con
MTTD medido**.

📄 Reporte ejecutivo: [`docs/Reporte_Lab_Integrador.pdf`](docs/Reporte_Lab_Integrador.pdf)

**Universidad de La Sabana — Maestría en Arquitectura de Software**
Observabilidad en Ambientes Productivos · Docente: Mg. María Fernanda Ochoa Paipilla

| Integrante | Código |
|---|---|
| Carlos Alberto Zambrano Braydi | 0000395349 |
| Henry Julián Salazar Salcedo | 0000396117 |
| Wolfran Alirio Pinzón Murillo | 0000393439 |

## Estructura

| Ruta | Contenido |
|---|---|
| `terraform/gcp/` | IaC: VPC + Flow Logs, GKE zonal (Dataplane V2, Workload Identity), Cloud SQL privado, Artifact Registry, APIs |
| `service-a/`, `service-b/`, `data-service/` | Microservicios FastAPI con OTel SDK (semconv HTTP y DB estables) |
| `base/` | Manifiestos K8s: namespace/RBAC, ConfigMaps (Collector, SQL), Deployments, Job `db-init` |
| `monitoring/prometheus-config.yaml` | Scrape + reglas SLI, AIOps (2σ, correlación) y estáticas (comparación de ruido) |
| `chaos-mesh/` | Experimento 1: NetworkChaos 200 ms en service-b |
| `scripts/deploy.sh` | Build (Cloud Build) y despliegue sobre los outputs de Terraform |
| `scripts/run_chaos.py` | Orquestador de caos: MTTD, enriquecimiento con `trace_id`, error budget, conteo de ruido |
| `k6/carga-constante.js` | Carga constante para los experimentos |
| `docs/` | Reporte y evidencias (`docs/evidencias/chaos/resultados.txt`) |

## Ejecución

```bash
# 1. Infraestructura (~15 min)
cd terraform/gcp && terraform init && terraform apply && cd ../..

# 2. Imágenes, aplicación y Chaos Mesh
./scripts/deploy.sh images
./scripts/deploy.sh app
./scripts/deploy.sh chaos-mesh

# 3. Prueba de humo
IP=$(kubectl get svc service-a-svc -n otel-lab -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
curl -s http://$IP:8000/order/ord-001

# 4. Experimentos de caos (port-forward de Prometheus :9092 y Jaeger :16686)
kubectl port-forward -n otel-lab svc/prometheus-svc 9092:9090 &
kubectl port-forward -n otel-lab svc/jaeger-svc 16686:16686 &
k6 run -e HOST=$IP k6/carga-constante.js &
python3 scripts/run_chaos.py | tee docs/evidencias/chaos/resultados.txt

# 5. Limpieza del sandbox
terraform -chdir=terraform/gcp destroy
```

## SLOs

| SLI | SLO | Fuente |
|---|---|---|
| Disponibilidad del borde (service-a, no-5xx) | 99,5 % / 30 días | `http.server.request.duration` (count por status) |
| Latencia p99 | < 500 ms | `http.server.request.duration` (histograma) |
