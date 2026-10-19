terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.25"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ── APIs del proyecto ───────────────────────────────────────────────────────
# disable_on_destroy = false: un `terraform destroy` del lab no debe apagar
# APIs del proyecto que otros recursos o personas puedan estar usando.
resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "sqladmin.googleapis.com",
    "servicenetworking.googleapis.com",
    "artifactregistry.googleapis.com",
    "containerscanning.googleapis.com",
    "cloudbuild.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# ── Red VPC dedicada ────────────────────────────────────────────────────────
resource "google_compute_network" "otel_vpc" {
  name                    = "otel-lab-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "otel_subnet" {
  name          = "otel-lab-subnet"
  ip_cidr_range = "10.10.0.0/20"
  region        = var.region
  network       = google_compute_network.otel_vpc.id

  secondary_ip_range {
    range_name    = "k8s-pod-range"
    ip_cidr_range = "10.20.0.0/16"
  }

  secondary_ip_range {
    range_name    = "k8s-service-range"
    ip_cidr_range = "10.30.0.0/20"
  }

  # Módulo C — VPC Flow Logs.
  # Intervalo de 5s y muestreo 100% para que el tráfico de la demo aparezca
  # en Cloud Logging en segundos. En producción real se usaría 0.1–0.5 de
  # muestreo para controlar el costo de ingestión.
  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 1.0
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# ── Clúster GKE (zonal) con Workload Identity y Dataplane V2 ────────────────
resource "google_container_cluster" "primary" {
  name = var.cluster_name
  # Zonal y no regional: un solo control plane y nodos en una zona.
  # Es 1/3 de los nodos de un clúster regional y el primer clúster zonal
  # no paga management fee — suficiente para un sandbox de laboratorio.
  location = var.zone

  network    = google_compute_network.otel_vpc.name
  subnetwork = google_compute_subnetwork.otel_subnet.name

  remove_default_node_pool = true
  initial_node_count       = 1

  # Dataplane V2 (eBPF/Cilium): habilita NetworkPolicy con logging de
  # conexiones denegadas — la señal de "tráfico anómalo E-W" del Módulo C.
  datapath_provider = "ADVANCED_DATAPATH"

  ip_allocation_policy {
    cluster_secondary_range_name  = "k8s-pod-range"
    services_secondary_range_name = "k8s-service-range"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS", "DEPLOYMENT"]
    managed_prometheus {
      enabled = true
    }
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  deletion_protection = false
}

# ── Node Pool dedicado ────────────────────────────────────────────────────────
resource "google_container_node_pool" "primary_nodes" {
  name       = "${var.cluster_name}-node-pool"
  location   = var.zone
  cluster    = google_container_cluster.primary.name
  node_count = var.node_count

  autoscaling {
    min_node_count = 1
    max_node_count = 3
  }

  node_config {
    machine_type = var.machine_type
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]

    labels = {
      environment = "production"
      app         = "otel-lab"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}

# ── Artifact Registry ────────────────────────────────────────────────────────
# Reemplaza a gcr.io (Container Registry, descontinuado). Con la API
# containerscanning habilitada, cada push se escanea y los CVEs quedan
# disponibles para el dashboard de seguridad (Módulo C).
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "lab-integrador"
  format        = "DOCKER"
  description   = "Imágenes de service-a, service-b y data-service"
}

# ── Cloud SQL (PostgreSQL) con IP privada ────────────────────────────────────
# Private Service Access: reserva un rango en la VPC y la conecta por peering
# con la red de servicios de Google. Así los pods llegan a Cloud SQL por IP
# privada, sin Cloud SQL Auth Proxy y sin exponer la DB a internet.
resource "google_compute_global_address" "private_ip_range" {
  name          = "otel-lab-sql-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = google_compute_network.otel_vpc.id
}

resource "google_service_networking_connection" "private_vpc_connection" {
  network                 = google_compute_network.otel_vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_ip_range.name]
}

resource "google_sql_database_instance" "postgres" {
  name             = "otel-lab-pg"
  database_version = "POSTGRES_16"
  region           = var.region

  depends_on = [google_service_networking_connection.private_vpc_connection]

  settings {
    # db-f1-micro solo existe en la edición ENTERPRISE; en POSTGRES_16 la
    # edición por defecto es ENTERPRISE_PLUS, por eso se fija explícitamente.
    edition           = "ENTERPRISE"
    tier              = var.db_tier
    availability_type = "ZONAL"
    disk_size         = 10

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.otel_vpc.id
    }

    # Query Insights: latencia y plan por query en la consola de Cloud SQL.
    # Complementa los DB spans de OTel con la vista del lado del servidor.
    insights_config {
      query_insights_enabled  = true
      record_application_tags = true
      record_client_address   = true
    }

    backup_configuration {
      enabled = false
    }
  }

  deletion_protection = false
}

resource "google_sql_database" "appdb" {
  name     = "appdb"
  instance = google_sql_database_instance.postgres.name
}

resource "random_password" "db_password" {
  length  = 24
  special = false
}

resource "google_sql_user" "app" {
  name     = "app"
  instance = google_sql_database_instance.postgres.name
  password = random_password.db_password.result
}

# ── Service Account para OTel Collector (Workload Identity) ──────────────────
resource "google_service_account" "otel_collector_sa" {
  account_id   = "otel-collector-sa"
  display_name = "Service Account para OpenTelemetry Collector"
}

# Permisos para Cloud Trace
resource "google_project_iam_member" "trace_agent" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.otel_collector_sa.email}"
}

# Permisos para Cloud Logging
resource "google_project_iam_member" "log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.otel_collector_sa.email}"
}

# Permisos para Cloud Monitoring (Métricas)
resource "google_project_iam_member" "metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.otel_collector_sa.email}"
}

# Vinculación Workload Identity con el ServiceAccount de Kubernetes
resource "google_service_account_iam_member" "workload_identity_binding" {
  service_account_id = google_service_account.otel_collector_sa.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[otel-lab/otel-collector-sa]"

  # El pool <project>.svc.id.goog lo crea GKE al crear el primer clúster con
  # Workload Identity. El member solo usa var.project_id, así que Terraform no
  # infiere la dependencia y crearía el binding en paralelo → "Identity Pool
  # does not exist". depends_on fuerza el orden.
  depends_on = [google_container_cluster.primary]
}
