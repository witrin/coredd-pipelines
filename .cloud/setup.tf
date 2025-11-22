terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

variable "cloudbuild_repository" {
  description = "Full resource name of the Cloud Build repository to watch for tag pushes."
  type        = string
  default     = "projects/bug-request/locations/europe-west1/connections/bug-request/repositories/bug-request-benchmark"
}

variable "github_org" {
  description = "GitHub Organization name to allow for sign-in"
  type        = string
  default     = "bug-request"
}

variable "github_argilla_callback" {
  description = "OAuth callback URL registered in GitHub for evaluation sign-in."
  type        = string
  default     = "https://evaluation.bug-request.dev/oauth/callback"
}

variable "log_retention_days" {
  description = "Number of days to retain logs in the default project logging bucket."
  type        = number
  default     = 30
}

provider "google" {
  project               = "bug-request"
  region                = local.region
  billing_project       = "bug-request"
  user_project_override = true
}

provider "google-beta" {
  project               = "bug-request"
  region                = local.region
  billing_project       = "bug-request"
  user_project_override = true
}

data "google_project" "current" {}

data "google_secret_manager_secret_version" "pipelines_database_password" {
  project = data.google_project.current.project_id
  secret  = "dagster-database-password"
  version = "latest"
}

data "google_secret_manager_secret_version" "github_dagster_client_id" {
  project = data.google_project.current.project_id
  secret  = "github-dagster-client-id"
  version = "latest"
}

data "google_secret_manager_secret_version" "github_dagster_client_secret" {
  project = data.google_project.current.project_id
  secret  = "github-dagster-client-secret"
  version = "latest"
}

data "google_secret_manager_secret_version" "github_argilla_client_id" {
  project = data.google_project.current.project_id
  secret  = "github-argilla-client-id"
  version = "latest"
}

data "google_secret_manager_secret_version" "github_argilla_client_secret" {
  project = data.google_project.current.project_id
  secret  = "github-argilla-client-secret"
  version = "latest"
}

data "google_secret_manager_secret_version" "github_guard_token" {
  project = data.google_project.current.project_id
  secret  = "github-guard-token"
  version = "latest"
}

data "google_secret_manager_secret_version" "oauth_cookie_secret" {
  project = data.google_project.current.project_id
  secret  = "dagster-oauth-cookie-secret"
  version = "latest"
}

data "google_compute_default_service_account" "compute_default" {
  project = data.google_project.current.project_id
}

data "google_app_engine_default_service_account" "app_engine" {
  project = data.google_project.current.project_id
}

locals {
  region                             = "europe-west1"
  zone                               = "${local.region}-b"
  artifact_registry_host             = format("%s-docker.pkg.dev", local.region)
  artifact_registry_repository       = "${local.artifact_registry_host}/${data.google_project.current.project_id}/containers"
  pipelines_image_repository         = "${local.artifact_registry_repository}/pipelines"
  code_image_repository              = "${local.artifact_registry_repository}/pipelines/code"
  job_image_repository              = "${local.artifact_registry_repository}/pipelines/job"
  evaluation_image_repository        = "${local.artifact_registry_repository}/evaluation"
  default_image_tag                  = "latest"
  trunk                              = "main"
  pipelines_image                    = "${local.pipelines_image_repository}:${local.default_image_tag}"
  unit_version                       = formatdate("2006-01-02", timestamp())
  cloud_build_service_account        = "${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
  compute_default_service_account    = data.google_compute_default_service_account.compute_default.email
  app_engine_default_service_account = data.google_app_engine_default_service_account.app_engine.email
  cloud_build_repository_resource    = var.cloudbuild_repository
  developer_connect_service_account  = google_project_service_identity.developer_connect.email
  cloud_sql_instance                 = google_sql_database_instance.database.connection_name
  required_services = [
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "certificatemanager.googleapis.com",
    "bigquery.googleapis.com",
    "bigquerydatapolicy.googleapis.com",
    "bigquerydatatransfer.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudfunctions.googleapis.com",
    "logging.googleapis.com",
    "dataform.googleapis.com",
    "dataplex.googleapis.com",
    "deploymentmanager.googleapis.com",
    "firebasehosting.googleapis.com",
    "firebaserules.googleapis.com",
    "securetoken.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
    "pubsub.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "developerconnect.googleapis.com",
    "oslogin.googleapis.com",
    "osconfig.googleapis.com",
    "iap.googleapis.com",
    "identitytoolkit.googleapis.com"
  ]
  project_number                = data.google_project.current.number
  pipelines_service_identifier  = tostring(google_compute_backend_service.pipelines_service.generated_id)
  iap_backend_path              = "projects/${local.project_number}/iap_web/compute/services/${local.pipelines_service_identifier}"
}

resource "google_project_service" "required" {
  for_each                   = toset(local.required_services)
  service                    = each.key
  disable_on_destroy         = false
  disable_dependent_services = false
}

# resource "google_logging_project_bucket_config" "default" {
#   project        = data.google_project.current.project_id
#   bucket_id      = "_Default"
#   location       = "global"
#   retention_days = var.log_retention_days

#   depends_on = [
#     google_project_service.required["logging.googleapis.com"]
#   ]
# }

resource "google_storage_bucket" "build" {
  name                        = "${data.google_project.current.project_id}-build"
  location                    = local.region
  uniform_bucket_level_access = true
  force_destroy               = true
}

resource "google_storage_bucket_iam_member" "cloudbuild_writer" {
  bucket = google_storage_bucket.build.name
  role   = "roles/storage.admin"
  member = "serviceAccount:${local.cloud_build_service_account}"
}

resource "google_storage_bucket_iam_member" "pipelines_runtime_writer" {
  bucket = google_storage_bucket.build.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cloud_run_runtime.email}"
}
