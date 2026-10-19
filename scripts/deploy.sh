#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
# deploy.sh — Despliegue de cargas sobre la infraestructura de Terraform
#
# Separación de responsabilidades:
#   terraform/gcp  → infraestructura (VPC, GKE, Cloud SQL, Artifact Registry)
#   este script    → lo que corre DENTRO del clúster (apps, stack OTel, caos)
#
# Uso:
#   ./scripts/deploy.sh images      # build (linux/amd64) + push a Artifact Registry
#   ./scripts/deploy.sh app         # credenciales, secret DB, manifiestos, init DB
#   ./scripts/deploy.sh chaos-mesh  # instala Chaos Mesh vía Helm
#   ./scripts/deploy.sh all         # images + app + chaos-mesh
#   ./scripts/deploy.sh ui          # port-forward de Jaeger, Prometheus y Chaos Mesh
#   ./scripts/deploy.sh status
# ════════════════════════════════════════════════════════════════
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$ROOT/terraform/gcp"
NAMESPACE="otel-lab"
VERSION="1.0.0"

GREEN='\033[0;32m'; BLUE='\033[0;34m'; RED='\033[0;31m'; NC='\033[0m'
log() { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $1"; }
ok()  { echo -e "${GREEN}✅ $1${NC}"; }
err() { echo -e "${RED}❌ $1${NC}"; exit 1; }

tf_out() { terraform -chdir="$TF_DIR" output -raw "$1"; }

images() {
  local repo; repo="$(tf_out image_repo)"
  gcloud auth configure-docker "${repo%%/*}" --quiet
  for svc in service-a service-b data-service; do
    log "Build $svc → $repo/$svc:$VERSION"
    # --platform linux/amd64: el Mac es arm64 y los nodos e2 de GKE son amd64.
    # Sin esto los pods fallan con "exec format error".
    docker buildx build --platform linux/amd64 \
      -t "$repo/$svc:$VERSION" --push "$ROOT/$svc"
  done
  ok "Imágenes publicadas"
}

app() {
  log "Conectando kubectl al clúster..."
  eval "$(tf_out get_credentials_command)"

  kubectl apply -f "$ROOT/base/00-namespace-rbac.yaml"

  # Workload Identity: el KSA del Collector actúa como el GSA de Terraform
  kubectl annotate serviceaccount otel-collector-sa -n "$NAMESPACE" --overwrite \
    iam.gke.io/gcp-service-account="$(tf_out collector_service_account_email)"

  # Secret con la conexión a Cloud SQL (IP privada). Se genera desde los
  # outputs de Terraform para que el password nunca quede en el repo.
  local db_url="postgresql://app:$(tf_out db_password)@$(tf_out db_private_ip):5432/appdb"
  kubectl create secret generic db-credentials -n "$NAMESPACE" \
    --from-literal=DATABASE_URL="$db_url" \
    --dry-run=client -o yaml | kubectl apply -f -

  kubectl apply -f "$ROOT/base/01-configmaps.yaml"
  kubectl apply -f "$ROOT/monitoring/prometheus-config.yaml"

  log "Inicializando esquema en Cloud SQL..."
  kubectl delete job db-init -n "$NAMESPACE" --ignore-not-found
  kubectl apply -f "$ROOT/base/03-db-init-job.yaml"
  kubectl wait --for=condition=complete job/db-init -n "$NAMESPACE" --timeout=180s
  ok "Esquema listo"

  kubectl apply -f "$ROOT/base/02-deployments.yaml"
  # Reinicia para tomar imágenes nuevas con el mismo tag (imagePullPolicy por defecto)
  kubectl rollout restart deployment/service-a deployment/service-b deployment/data-service -n "$NAMESPACE"
  for d in service-a service-b data-service; do
    kubectl rollout status deployment/"$d" -n "$NAMESPACE" --timeout=300s
  done
  ok "Aplicación desplegada"
  kubectl get svc service-a-svc -n "$NAMESPACE"
}

chaos_mesh() {
  helm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null 2>&1 || true
  helm repo update chaos-mesh
  helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
    --namespace chaos-mesh --create-namespace \
    --set chaosDaemon.runtime=containerd \
    --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
    --set dashboard.securityMode=false \
    --wait --timeout=5m
  ok "Chaos Mesh instalado"
}

ui() {
  log "Jaeger http://localhost:16686 · Prometheus http://localhost:9091 · Chaos Mesh http://localhost:2333"
  kubectl port-forward -n "$NAMESPACE" svc/jaeger-svc 16686:16686 &
  kubectl port-forward -n "$NAMESPACE" svc/prometheus-svc 9091:9090 &
  kubectl port-forward -n chaos-mesh svc/chaos-dashboard 2333:2333 &
  wait
}

status() {
  kubectl get pods,svc -n "$NAMESPACE" -o wide
}

case "${1:-}" in
  images)     images ;;
  app)        app ;;
  chaos-mesh) chaos_mesh ;;
  all)        images; app; chaos_mesh ;;
  ui)         ui ;;
  status)     status ;;
  *)          err "Uso: $0 {images|app|chaos-mesh|all|ui|status}" ;;
esac
