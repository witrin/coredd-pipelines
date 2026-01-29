resource "google_artifact_registry_repository" "containers" {
  location      = local.region
  repository_id = "containers"
  format        = "DOCKER"
  description   = "Container images for the project."
  depends_on    = [google_project_service.required["artifactregistry.googleapis.com"]]

  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "259200s"
    }
  }

  cleanup_policies {
    id     = "delete-old-pipeline-images"
    action = "DELETE"
    condition {
      tag_state             = "TAGGED"
      older_than            = "604800s"
      package_name_prefixes = ["pipelines/"]
    }
  }

  cleanup_policies {
    id     = "keep-nested-pipeline-images"
    action = "KEEP"
    condition {
      tag_state             = "TAGGED"
      package_name_prefixes = ["pipelines/code", "pipelines/job"]
    }
  }

  cleanup_policies {
    id     = "keep-latest"
    action = "KEEP"
    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["latest"]
    }
  }
}

resource "google_artifact_registry_repository" "apt" {
  location      = local.region
  repository_id = "apt"
  format        = "APT"
  description   = "Host packages for compute instances."
  depends_on    = [google_project_service.required["artifactregistry.googleapis.com"]]
}

resource "google_artifact_registry_repository" "packages" {
  location      = local.region
  repository_id = "packages"
  format        = "PYTHON"
  description   = "Python packages."
  depends_on    = [google_project_service.required["artifactregistry.googleapis.com"]]
}

output "registry" {
  value = {
    containers = "${local.region}-docker.pkg.dev/${data.google_project.current.project_id}/${google_artifact_registry_repository.containers.repository_id}"
    packages   = google_artifact_registry_repository.apt.repository_id
  }
}

output "computation" {
  value = {
    image = "projects/${data.google_project.current.project_id}/global/images/family/computation"
  }
}
