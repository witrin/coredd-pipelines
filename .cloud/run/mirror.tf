variable "mirror_image_tag" {
  description = "Tag of the mirror image in the containers registry; the image carries the mirror root built by benchmark.mirror.archive."
  type        = string
  default     = "2026-11-24.1"
}

resource "google_service_account" "mirror" {
  account_id   = "mirror"
  display_name = "Mirror"
  description  = "Runtime identity of the read-only repository mirror. It holds no roles, since the mirror reads only data baked into its image."
}

# The mirror has no sign-in on purpose: annotators pass its links to chat assistants whose
# fetchers cannot sign in. One instance at most caps the cost of unwanted traffic, and
# robots.txt plus noindex keep crawlers out.
resource "google_cloud_run_v2_service" "mirror" {
  name                = "mirror"
  location            = local.region
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"

  template {
    containers {
      image = "${local.artifact_registry_repository}/mirror:${var.mirror_image_tag}"

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1000m"
          memory = "1Gi"
        }
        cpu_idle = true
      }

      startup_probe {
        http_get {
          path = "/robots.txt"
        }
      }
    }

    service_account                  = google_service_account.mirror.email
    max_instance_request_concurrency = 16
    timeout                          = "60s"

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "mirror" {
  project  = data.google_project.current.project_id
  location = local.region
  name     = google_cloud_run_v2_service.mirror.name

  role   = "roles/run.invoker"
  member = "allUsers"
}

resource "google_cloud_run_domain_mapping" "mirror" {
  location = local.region
  name     = "mirror.bug-request.dev"

  metadata {
    namespace = data.google_project.current.project_id
  }

  spec {
    route_name = google_cloud_run_v2_service.mirror.name
  }
}
