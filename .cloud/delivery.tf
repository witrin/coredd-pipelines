resource "google_cloudbuild_trigger" "delivery" {
  name        = "delivery"
  description = "Builds branch-specific Dagster code images."
  location    = local.region
  filename    = ".cloud/delivery/delivery.yaml"

  repository_event_config {
    repository = local.cloud_build_repository_resource
    push {
      branch = ".*"
    }
  }

  included_files = [
    ".dagster/**",
    "src/**",
    "Dockerfile",
    "pyproject.toml",
    "requirements.lock"
  ]

  substitutions = {
    "_REGION"     = local.region
    "_REPOSITORY" = google_artifact_registry_repository.containers.repository_id
  }

  service_account = "projects/${data.google_project.current.project_id}/serviceAccounts/${google_service_account.cloud_run_runtime.email}"

  depends_on = [google_project_service.required["cloudbuild.googleapis.com"]]

  lifecycle {
    ignore_changes = [
      repository_event_config,
      service_account
    ]
  }
}
