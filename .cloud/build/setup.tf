terraform {
  backend "local" {
    path = "../.states/build.tfstate"
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

locals {
  region                             = "europe-west1"
  zone                               = "${local.region}-b"
  build_service_account              = google_service_account.build.email
  cloud_build_repository_resource    = var.cloudbuild_repository
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
}

resource "google_project_service" "required" {
  for_each                   = toset(local.required_services)
  service                    = each.key
  disable_on_destroy         = false
  disable_dependent_services = false
}
