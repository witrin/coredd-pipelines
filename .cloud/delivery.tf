resource "google_artifact_registry_repository" "packages" {
  location      = local.region
  repository_id = "packages"
  format        = "PYTHON"
  description   = "Python packages."
  depends_on    = [google_project_service.required["artifactregistry.googleapis.com"]]
}

resource "google_cloudbuild_trigger" "packages" {
  name        = "packages"
  description = "Builds and publishes packages."
  location    = local.region
  filename    = ".cloud/delivery/packages/delivery.yaml"

  repository_event_config {
    repository = local.cloud_build_repository_resource
    push {
      branch = "^main$"
    }
  }

  included_files = [
    ".cloud/delivery/packages/**",
    ".jupyter/**"
  ]

  substitutions = {
    "_REGION"            = local.region
    "_REPOSITORY" = google_artifact_registry_repository.packages.repository_id
  }

  service_account = "projects/${data.google_project.current.project_id}/serviceAccounts/${local.build_service_account}"

  depends_on = [
    google_project_service.required["cloudbuild.googleapis.com"],
    google_artifact_registry_repository.packages
  ]

  lifecycle {
    ignore_changes = [
      repository_event_config
    ]
  }
}

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

  service_account = "projects/${data.google_project.current.project_id}/serviceAccounts/${local.build_service_account}"

  depends_on = [google_project_service.required["cloudbuild.googleapis.com"]]

  lifecycle {
    ignore_changes = [
      repository_event_config
    ]
  }
}
