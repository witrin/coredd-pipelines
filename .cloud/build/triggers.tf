resource "google_cloudbuild_trigger" "packages" {
  name        = "packages"
  description = "Builds and publishes packages."
  location    = local.region
  filename    = ".cloud/build/delivery/packages/delivery.yaml"

  repository_event_config {
    repository = local.cloud_build_repository_resource
    push {
      branch = "^main$"
    }
  }

  included_files = [
    ".cloud/build/delivery/packages/**",
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
  filename    = ".cloud/build/delivery/delivery.yaml"

  repository_event_config {
    repository = local.cloud_build_repository_resource
    push {
      branch = ".*"
    }
  }

  included_files = [
    ".cloud/**",
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

resource "google_cloudbuild_trigger" "deploy" {
  name        = "computation"
  description = "Builds and deploys the pipeline computation when new commits are pushed."
  location    = local.region
  filename    = ".cloud/build/computation/delivery.yaml"

  repository_event_config {
    repository = local.cloud_build_repository_resource
    push {
      branch = "^main$"
    }
  }

  included_files = [
    ".argilla/**",
    ".dagster/**",
    ".cloud/build/computation/**",
    ".cloud/build/delivery/**",
    ".cloud/build/identity/**",
    "Dockerfile",
    "pyproject.toml",
    "requirements.lock"
  ]

  substitutions = {
    "_REGION"   = local.region
    "_IMAGES"   = google_artifact_registry_repository.containers.repository_id
    "_PACKAGES" = google_artifact_registry_repository.apt.repository_id
    "_IMAGE"    = "pipelines"
    "_TAG"      = "$SHORT_SHA"
    "_ZONE"     = local.zone
  }

  service_account = "projects/${data.google_project.current.project_id}/serviceAccounts/${local.build_service_account}"

  depends_on = [google_project_service.required["cloudbuild.googleapis.com"]]

  lifecycle {
    ignore_changes = [
      repository_event_config
    ]
  }
}

resource "google_cloudbuild_trigger" "identity" {
  name        = "identity"
  description = "Deploys the signup blocking function when new commits are pushed."
  location    = local.region
  filename    = ".cloud/build/identity/delivery.yaml"

  repository_event_config {
    repository = local.cloud_build_repository_resource
    push {
      branch = "^main$"
    }
  }

  substitutions = {
    "_REGION"       = local.region
    "_GITHUB_ORG"   = var.github_org
    "_BUILD_BUCKET" = google_storage_bucket.build.name
  }

  service_account = "projects/${data.google_project.current.project_id}/serviceAccounts/${local.build_service_account}"

  depends_on = [google_project_service.required["cloudbuild.googleapis.com"]]

  lifecycle {
    ignore_changes = [
      repository_event_config
    ]
  }
}
