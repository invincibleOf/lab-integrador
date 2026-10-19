variable "project_id" {
  description = "El ID del proyecto de Google Cloud"
  type        = string
  default     = "bold-bastion-505423-e7"
}

variable "region" {
  description = "Región de GCP (Cloud SQL, Artifact Registry, subred)"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Zona del clúster GKE zonal"
  type        = string
  default     = "us-central1-a"
}

variable "cluster_name" {
  description = "Nombre del clúster GKE"
  type        = string
  default     = "otel-lab"
}

variable "node_count" {
  description = "Número inicial de nodos en el Node Pool"
  type        = number
  default     = 2
}

variable "machine_type" {
  description = "Tipo de máquina para los nodos de GKE"
  type        = string
  default     = "e2-standard-4"
}

variable "db_tier" {
  description = "Tier de la instancia Cloud SQL"
  type        = string
  default     = "db-f1-micro"
}
