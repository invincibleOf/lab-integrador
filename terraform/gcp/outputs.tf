output "cluster_name" {
  description = "Nombre del clúster GKE creado"
  value       = google_container_cluster.primary.name
}

output "get_credentials_command" {
  description = "Comando para autenticar kubectl con el clúster"
  value       = "gcloud container clusters get-credentials ${google_container_cluster.primary.name} --zone=${var.zone} --project=${var.project_id}"
}

output "image_repo" {
  description = "Prefijo de las imágenes en Artifact Registry"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "db_private_ip" {
  description = "IP privada de Cloud SQL (accesible desde los pods)"
  value       = google_sql_database_instance.postgres.private_ip_address
}

output "db_password" {
  description = "Password del usuario app en Cloud SQL"
  value       = random_password.db_password.result
  sensitive   = true
}

output "collector_service_account_email" {
  description = "Email del Service Account de GCP para el OTel Collector"
  value       = google_service_account.otel_collector_sa.email
}
