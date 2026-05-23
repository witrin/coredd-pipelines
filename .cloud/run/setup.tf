terraform {
  backend "local" {
    path = "../.states/run.tfstate"
  }

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

data "terraform_remote_state" "build" {
  backend = "local"
  config = {
    path = "../.states/build.tfstate"
  }
}

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

data "google_secret_manager_secret_version" "github_dagster_api_secret" {
  project = data.google_project.current.project_id
  secret  = "github-dagster-api-secret"
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

data "google_secret_manager_secret_version" "together_ai_api_key" {
  project = data.google_project.current.project_id
  secret  = "pipelines-together-ai-api-key"
  version = "latest"
}

data "google_secret_manager_secret_version" "pipelines_argilla_api_key" {
  project = data.google_project.current.project_id
  secret  = "pipelines-argilla-api-key"
  version = "latest"
}

data "google_compute_default_service_account" "compute_default" {
  project = data.google_project.current.project_id
}

data "google_app_engine_default_service_account" "app_engine" {
  project = data.google_project.current.project_id
}

locals {
  secrets_prefix                     = "pipelines"
  region                             = "europe-west1"
  zone                               = "${local.region}-b"
  artifact_registry_host             = format("%s-docker.pkg.dev", local.region)
  artifact_registry_repository       = data.terraform_remote_state.build.outputs.registry.containers
  pipelines_image_repository         = "${local.artifact_registry_repository}/pipelines"
  code_image_repository              = "${local.artifact_registry_repository}/pipelines/code"
  job_image_repository               = "${local.artifact_registry_repository}/pipelines/job"
  evaluation_image_repository        = "${local.artifact_registry_repository}/evaluation"
  pipelines_image                    = "${local.pipelines_image_repository}:latest"
  unit_version                       = formatdate("2006-01-02", timestamp())
  build_service_account              = data.terraform_remote_state.build.outputs.account
  compute_default_service_account    = data.google_compute_default_service_account.compute_default.email
  app_engine_default_service_account = data.google_app_engine_default_service_account.app_engine.email
  developer_connect_service_account  = google_project_service_identity.developer_connect.email
  cloud_sql_instance                 = google_sql_database_instance.database.connection_name
  project_number                = data.google_project.current.number
  pipelines_service_identifier  = tostring(google_compute_backend_service.pipelines_service.generated_id)
  iap_backend_path              = "projects/${local.project_number}/iap_web/compute/services/${local.pipelines_service_identifier}"
}

resource "google_storage_bucket_iam_member" "pipelines_runtime_writer" {
  bucket = data.terraform_remote_state.build.outputs.bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime.email}"
}
