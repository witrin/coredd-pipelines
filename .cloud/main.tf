terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "ghcr_image" {
  description = "Fully qualified GHCR image reference to mirror (e.g. ghcr.io/org/app:tag)."
  type        = string
  default     = "ghcr.io/bug-request/benchmark:latest"
}

variable "ghcr_user" {
  description = "GitHub username or organization name used for GHCR authentication."
  type        = string
  default     = "bug-request"
}

variable "ghcr_secret_manager" {
  description = "Secret Manager ID that stores the GHCR personal access token."
  type        = string
  default     = "ghcr-token"
}

provider "google" {
  project = "bug-request"
  region  = "europe-west3"
}

data "google_project" "current" {}

locals {
  region                             = "europe-west3"
  network_name                       = "bug-request-network"
  subnet_name                        = "bug-request-subnet"
  subnet_cidr                        = "10.20.0.0/24"
  vpc_connector_name                 = "bug-request-connector"
  connector_subnet_name              = "bug-request-connector-subnet"
  connector_subnet_cidr              = "10.20.1.0/28"
  private_service_range_name         = "bug-request-private-range"
  artifact_repository                = "containers"
  artifact_image_name                = "benchmark"
  artifact_image_tag                 = "latest"
  artifact_registry_host             = "${local.region}-docker.pkg.dev"
  artifact_registry_image_reference  = "${local.artifact_registry_host}/${data.google_project.current.project_id}/${local.artifact_repository}/${local.artifact_image_name}:${local.artifact_image_tag}"
  cloud_build_service_account        = "${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
  compute_default_service_account    = "${data.google_project.current.number}-compute@developer.gserviceaccount.com"
  cloud_sql_service_account          = "service-${data.google_project.current.number}@gcp-sa-cloud-sql.iam.gserviceaccount.com"
  required_services                  = [
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "sqladmin.googleapis.com",
    "servicenetworking.googleapis.com",
    "vpcaccess.googleapis.com",
    "pubsub.googleapis.com"
  ]
}

resource "google_compute_network" "bug_request" {
  name                    = local.network_name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "bug_request" {
  name          = local.subnet_name
  ip_cidr_range = local.subnet_cidr
  region        = local.region
  network       = google_compute_network.bug_request.id
}

resource "google_compute_subnetwork" "bug_request_connector" {
  name          = local.connector_subnet_name
  ip_cidr_range = local.connector_subnet_cidr
  region        = local.region
  network       = google_compute_network.bug_request.id
}

resource "google_project_service" "required" {
  for_each            = toset(local.required_services)
  service             = each.key
  disable_on_destroy  = false
  disable_dependent_services = false
}

resource "google_artifact_registry_repository" "containers" {
  location      = local.region
  repository_id = local.artifact_repository
  format        = "DOCKER"
  description   = "Container images for the bug-request project."
  depends_on    = [google_project_service.required["artifactregistry.googleapis.com"]]
}

resource "google_compute_global_address" "private_service_range" {
  name          = local.private_service_range_name
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = google_compute_network.bug_request.id
  depends_on    = [google_project_service.required["servicenetworking.googleapis.com"]]
}

resource "google_service_networking_connection" "private_vpc_connection" {
  network                 = google_compute_network.bug_request.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_service_range.name]
  depends_on              = [google_project_service.required["servicenetworking.googleapis.com"]]
}

resource "google_vpc_access_connector" "bug_request" {
  name        = local.vpc_connector_name
  region      = local.region
  subnet {
    name = google_compute_subnetwork.bug_request_connector.name
  }
  machine_type = "e2-micro"
  min_throughput = 200
  max_throughput = 300
  depends_on   = [google_project_service.required["vpcaccess.googleapis.com"]]
}

resource "google_service_account" "cloud_run_runtime" {
  account_id   = "bug-request-cloud-run"
  display_name = "Bug Request Cloud Run runtime"
}

resource "google_service_account" "cloud_build_trigger" {
  account_id   = "cloud-build-trigger"
  display_name = "Cloud Build trigger runtime"
}

resource "google_project_iam_member" "cloud_run_artifact_reader" {
  project = data.google_project.current.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.cloud_run_runtime.email}"
}

resource "google_project_iam_member" "cloud_build_trigger_builder" {
  project = data.google_project.current.project_id
  role    = "roles/cloudbuild.builds.builder"
  member  = "serviceAccount:${google_service_account.cloud_build_trigger.email}"
}

resource "google_project_iam_member" "cloud_build_trigger_storage" {
  project = data.google_project.current.project_id
  role    = "roles/storage.admin"
  member  = "serviceAccount:${google_service_account.cloud_build_trigger.email}"
}

resource "google_project_iam_member" "cloud_build_trigger_logging" {
  project = data.google_project.current.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.cloud_build_trigger.email}"
}

resource "google_project_iam_member" "cloud_build_trigger_serviceusage" {
  project = data.google_project.current.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = "serviceAccount:${google_service_account.cloud_build_trigger.email}"
}

resource "google_project_iam_member" "cloud_run_sql_client" {
  project = data.google_project.current.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.cloud_run_runtime.email}"
}

resource "google_project_iam_member" "cloud_build_artifact_writer" {
  project = data.google_project.current.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${local.cloud_build_service_account}"
}

resource "google_project_iam_member" "compute_build_artifact_writer" {
  project = data.google_project.current.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${local.compute_default_service_account}"
}

resource "google_project_iam_member" "cloud_build_trigger_artifact_writer" {
  project = data.google_project.current.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.cloud_build_trigger.email}"
}

resource "google_storage_bucket" "bug_request_bucket" {
  name          = "bug-request-bucket"
  location      = "EU"
  storage_class = "STANDARD"
  force_destroy = true
  depends_on    = [google_project_service.required["artifactregistry.googleapis.com"]]
}

resource "google_sql_database_instance" "bug_request_database" {
  name             = "bug-request-database"
  database_version = "POSTGRES_15"
  region           = "europe-west3"
  depends_on       = [
    google_service_networking_connection.private_vpc_connection,
    google_project_service.required["sqladmin.googleapis.com"]
  ]

  settings {
    tier       = "db-f1-micro"
    disk_type  = "PD_SSD"
    disk_size  = 10
    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.bug_request.id
    }
  }
}

resource "google_sql_database" "dagster_meta" {
  name     = "dagster_meta"
  instance = google_sql_database_instance.bug_request_database.name
}

resource "google_sql_user" "dagster_user" {
  name     = "dagster"
  instance = google_sql_database_instance.bug_request_database.name
  password = "dagster-secret"
}

resource "google_cloud_run_v2_service" "bug_request_daemon" {
  name     = "bug-request-daemon"
  location = local.region
  depends_on = [
    google_vpc_access_connector.bug_request,
    google_project_service.required["run.googleapis.com"]
  ]

  deletion_protection = false

  template {
    service_account = google_service_account.cloud_run_runtime.email

    containers {
      image = local.artifact_registry_image_reference

      env {
        name  = "DAGSTER_HOME"
        value = "/var/lib/dagster"
      }
      env {
        name  = "PG_HOST"
        value = "/cloudsql/${google_sql_database_instance.bug_request_database.connection_name}"
      }
      env {
        name  = "PG_USER"
        value = google_sql_user.dagster_user.name
      }
      env {
        name  = "PG_PASSWORD"
        value = google_sql_user.dagster_user.password
      }
      env {
        name  = "PG_DATABASE"
        value = google_sql_database.dagster_meta.name
      }
      env {
        name  = "GCS_BUCKET"
        value = google_storage_bucket.bug_request_bucket.name
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          memory = "2Gi"
          cpu    = "1"
        }
      }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.bug_request_database.connection_name]
      }
    }

    vpc_access {
      connector = google_vpc_access_connector.bug_request.id
      egress    = "ALL_TRAFFIC"
    }

    scaling {
      max_instance_count = 1
    }
  }
}

resource "google_secret_manager_secret" "ghcr_token" {
  secret_id = var.ghcr_secret_manager
  replication {
    auto {}
  }
  depends_on = [google_project_service.required["secretmanager.googleapis.com"]]
}

resource "google_secret_manager_secret_iam_member" "ghcr_token_cloud_build" {
  project   = data.google_project.current.project_id
  secret_id = google_secret_manager_secret.ghcr_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.cloud_build_service_account}"
  depends_on = [
    google_secret_manager_secret.ghcr_token,
    google_project_service.required["secretmanager.googleapis.com"]
  ]
}

resource "google_secret_manager_secret_iam_member" "ghcr_token_compute" {
  project   = data.google_project.current.project_id
  secret_id = google_secret_manager_secret.ghcr_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.compute_default_service_account}"
  depends_on = [
    google_secret_manager_secret.ghcr_token,
    google_project_service.required["secretmanager.googleapis.com"]
  ]
}

resource "google_secret_manager_secret_iam_member" "ghcr_token_trigger" {
  project   = data.google_project.current.project_id
  secret_id = google_secret_manager_secret.ghcr_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cloud_build_trigger.email}"
  depends_on = [
    google_secret_manager_secret.ghcr_token,
    google_project_service.required["secretmanager.googleapis.com"]
  ]
}

resource "google_pubsub_topic" "mirror_ghcr" {
  name = "mirror-ghcr"
  depends_on = [google_project_service.required["pubsub.googleapis.com"]]
}

resource "google_pubsub_topic_iam_member" "mirror_ghcr_cloud_build" {
  topic  = google_pubsub_topic.mirror_ghcr.name
  role   = "roles/pubsub.subscriber"
  member = "serviceAccount:${local.cloud_build_service_account}"
}

resource "google_cloudbuild_trigger" "mirror_ghcr" {
  name        = "mirror-ghcr"
  description = "Mirrors the private GHCR image into Artifact Registry for Cloud Run."
  location    = "global"
  service_account = "projects/${data.google_project.current.project_id}/serviceAccounts/${google_service_account.cloud_build_trigger.email}"
  depends_on = [
    google_project_service.required["cloudbuild.googleapis.com"],
    google_secret_manager_secret_iam_member.ghcr_token_cloud_build,
    google_secret_manager_secret_iam_member.ghcr_token_trigger,
    google_project_iam_member.cloud_build_trigger_builder,
    google_project_iam_member.cloud_build_trigger_storage,
    google_project_iam_member.cloud_build_trigger_logging,
    google_project_iam_member.cloud_build_trigger_serviceusage,
    google_project_iam_member.cloud_build_trigger_artifact_writer,
    google_pubsub_topic.mirror_ghcr
  ]

  pubsub_config {
    topic = google_pubsub_topic.mirror_ghcr.id
  }

  build {
    substitutions = {
      "_GHCR_IMAGE"    = var.ghcr_image
      "_GHCR_USER"     = var.ghcr_user
      "_AR_IMAGE"      = local.artifact_registry_image_reference
      "_AR_HOST"       = local.artifact_registry_host
    }

    available_secrets {
      secret_manager {
        env          = "GHCR_TOKEN"
        version_name = "projects/${data.google_project.current.project_id}/secrets/${var.ghcr_secret_manager}/versions/latest"
      }
    }

    step {
      name       = "gcr.io/google.com/cloudsdktool/cloud-sdk"
      entrypoint = "gcloud"
      args       = ["auth", "configure-docker", "$${_AR_HOST}", "--quiet"]
    }

    step {
      name       = "gcr.io/cloud-builders/docker"
      entrypoint = "bash"
      args = [
        "-c",
        "echo \"$$GHCR_TOKEN\" | docker login ghcr.io -u $${_GHCR_USER} --password-stdin"
      ]
      secret_env = ["GHCR_TOKEN"]
    }

    step {
      name = "gcr.io/cloud-builders/docker"
      args = ["pull", "$${_GHCR_IMAGE}"]
    }

    step {
      name = "gcr.io/cloud-builders/docker"
      args = [
        "tag",
        "$${_GHCR_IMAGE}",
        "$${_AR_IMAGE}"
      ]
    }

    step {
      name = "gcr.io/cloud-builders/docker"
      args = ["push", "$${_AR_IMAGE}"]
    }
  }
}
