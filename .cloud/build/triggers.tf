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
    "_REGION"     = local.region
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

resource "google_cloudbuild_trigger" "locations" {
  name        = "locations"
  description = "Builds branch-specific Dagster code locations images."
  location    = local.region
  filename    = ".cloud/build/computation/dagster/locations/delivery.yaml"

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

resource "google_cloudbuild_trigger" "argilla" {
  name        = "argilla"
  description = "Builds and deploys the Argilla evaluation service."
  location    = local.region
  filename    = ".cloud/build/computation/argilla/delivery.yaml"

  repository_event_config {
    repository = local.cloud_build_repository_resource
    push {
      branch = "^main$"
    }
  }

  included_files = [
    ".argilla/**",
    ".cloud/build/computation/argilla/**",
    "Dockerfile",
    "pyproject.toml",
    "requirements.lock"
  ]

  substitutions = {
    "_REGION"   = local.region
    "_IMAGES"   = google_artifact_registry_repository.containers.repository_id
    "_PACKAGES" = google_artifact_registry_repository.apt.repository_id
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

resource "google_cloudbuild_trigger" "dagster" {
  name        = "dagster"
  description = "Builds and deploys the Dagster pipeline service."
  location    = local.region
  filename    = ".cloud/build/computation/dagster/delivery.yaml"

  repository_event_config {
    repository = local.cloud_build_repository_resource
    push {
      branch = "^main$"
    }
  }

  included_files = [
    ".dagster/**",
    ".cloud/build/computation/dagster/**",
    "Dockerfile",
    "pyproject.toml",
    "requirements.lock"
  ]

  substitutions = {
    "_REGION"   = local.region
    "_IMAGES"   = google_artifact_registry_repository.containers.repository_id
    "_PACKAGES" = google_artifact_registry_repository.apt.repository_id
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

resource "google_cloudbuild_trigger" "engine" {
  name        = "engine"
  description = "Builds and deploys the computation VM image."
  location    = local.region
  filename    = ".cloud/build/computation/engine/delivery.yaml"

  repository_event_config {
    repository = local.cloud_build_repository_resource
    push {
      branch = "^main$"
    }
  }

  included_files = [
    ".cloud/build/delivery/**",
    ".cloud/build/computation/engine/**"
  ]

  substitutions = {
    "_REGION"   = local.region
    "_IMAGES"   = google_artifact_registry_repository.containers.repository_id
    "_PACKAGES" = google_artifact_registry_repository.apt.repository_id
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
