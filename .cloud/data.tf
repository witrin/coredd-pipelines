resource "google_storage_bucket" "bucket" {
  name          = "bug-request-bucket"
  location      = "EU"
  storage_class = "STANDARD"
  force_destroy = true
  labels = {
    component = "bucket"
  }
  depends_on = [google_project_service.required["artifactregistry.googleapis.com"]]
}

resource "google_storage_bucket_iam_member" "pipelines_runtime_bucket_writer" {
  bucket = google_storage_bucket.bucket.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cloud_run_runtime.email}"
}

resource "google_sql_database_instance" "database" {
  name             = "bug-request-database"
  database_version = "POSTGRES_15"
  region           = local.region
  depends_on       = [google_project_service.required["sqladmin.googleapis.com"]]

  deletion_protection = false

  settings {
    tier      = "db-f1-micro"
    disk_type = "PD_HDD"
    disk_size = 10

    user_labels = {
      component = "database"
    }

    ip_configuration {
      ipv4_enabled = true
      authorized_networks {
        name  = "engine"
        value = google_compute_address.engine.address
      }
    }
  }
}

resource "null_resource" "database_credentials_hash" {
  triggers = {
    hash = sha1(jsonencode({
      user = google_sql_user.pipelines_user.name
      pass = data.google_secret_manager_secret_version.pipelines_database_password.secret_data
    }))
  }
}

resource "google_sql_database" "pipelines_meta" {
  name     = "dagster"
  instance = google_sql_database_instance.database.name
}

resource "google_sql_database" "evaluation_meta" {
  name     = "argilla"
  instance = google_sql_database_instance.database.name
}

resource "google_sql_user" "pipelines_user" {
  name     = "dagster"
  instance = google_sql_database_instance.database.name
  password = data.google_secret_manager_secret_version.pipelines_database_password.secret_data
}
