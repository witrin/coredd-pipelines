resource "google_service_account" "build" {
  account_id   = "pipelines-build"
  display_name = "Pipelines Cloud Build"
}

locals {
  roles = [
    "roles/artifactregistry.writer",
    "roles/artifactregistry.repoAdmin",
    "roles/cloudfunctions.developer",
    "roles/logging.logWriter",
    "roles/run.admin",
    "roles/osconfig.osPolicyAssignmentAdmin",
    "roles/compute.instanceAdmin.v1"
  ]
}

resource "google_project_iam_member" "roles" {
  for_each = toset(local.roles)
  project  = data.google_project.current.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.build.email}"
}

resource "google_service_account_iam_member" "build_act_as" {
  service_account_id = google_service_account.build.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
}

resource "google_secret_manager_secret_iam_member" "build_github_api_token" {
  secret_id = data.google_secret_manager_secret.build_github_api_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.build_service_account}"
}

resource "google_storage_bucket" "build" {
  name                        = "${data.google_project.current.project_id}-build"
  location                    = local.region
  uniform_bucket_level_access = true
  force_destroy               = true
}

resource "google_storage_bucket_iam_member" "build_writer" {
  bucket = google_storage_bucket.build.name
  role   = "roles/storage.admin"
  member = "serviceAccount:${local.build_service_account}"
}

output "account" {
  value = google_service_account.build.email
}

output "bucket" {
  value = google_storage_bucket.build.name
}
