resource "google_service_account" "cloud_run_runtime" {
  account_id   = "pipelines-runtime"
  display_name = "Pipelines Cloud Run runtime"
}

resource "google_artifact_registry_repository" "containers" {
  location      = local.region
  repository_id = "containers"
  format        = "DOCKER"
  description   = "Container images for the project."
  depends_on    = [google_project_service.required["artifactregistry.googleapis.com"]]
}

resource "google_cloudbuild_trigger" "deploy" {
  name        = "computation"
  description = "Builds and deploys the pipelines image when new commits are pushed."
  location    = local.region
  filename    = ".cloud/computation/delivery.yaml"

  repository_event_config {
    repository = local.cloud_build_repository_resource
    push {
      branch = "^main$"
    }
  }

  substitutions = {
    "_REGION"     = local.region
    "_REPOSITORY" = google_artifact_registry_repository.containers.repository_id
    "_IMAGE"      = "pipelines"
    "_TAG"        = "$SHORT_SHA"
    "_ZONE"       = local.zone
  }

  service_account = "projects/${data.google_project.current.project_id}/serviceAccounts/${google_service_account.cloud_run_runtime.email}"

  depends_on = [google_project_service.required["cloudbuild.googleapis.com"]]

  lifecycle {
    ignore_changes = [
      repository_event_config,
      service_account
    ]
  }
}

locals {
  iam_service_accounts = {
    runtime            = google_service_account.cloud_run_runtime.email
    cloud_build        = local.cloud_build_service_account
    compute_default    = local.compute_default_service_account
    app_engine_default = local.app_engine_default_service_account
  }

  iam_project_roles = {
    runtime = [
      "roles/artifactregistry.reader",
      "roles/artifactregistry.writer",
      "roles/cloudfunctions.developer",
      "roles/cloudsql.client",
      "roles/logging.logWriter",
      "roles/run.admin",
      "roles/osconfig.guestPolicyAdmin",
      "roles/compute.instanceAdmin.v1"
    ]
    cloud_build = [
      "roles/artifactregistry.writer",
      "roles/cloudfunctions.developer",
      "roles/run.admin",
      "roles/compute.instanceAdmin.v1"
    ]
    compute_default = [
      "roles/artifactregistry.reader",
      "roles/logging.logWriter"
    ]
  }

  iam_secret_access = {
    runtime = [
      "dagster-database-password",
      data.google_secret_manager_secret_version.oauth_cookie_secret.secret,
      data.google_secret_manager_secret_version.github_dagster_client_id.secret,
      data.google_secret_manager_secret_version.github_dagster_client_secret.secret,
      data.google_secret_manager_secret_version.github_guard_token.secret
    ]
    compute_default = [
      "dagster-database-password",
      data.google_secret_manager_secret_version.oauth_cookie_secret.secret,
      data.google_secret_manager_secret_version.github_dagster_client_id.secret,
      data.google_secret_manager_secret_version.github_dagster_client_secret.secret,
      data.google_secret_manager_secret_version.github_argilla_client_id.secret,
      data.google_secret_manager_secret_version.github_argilla_client_secret.secret,
      data.google_secret_manager_secret_version.github_guard_token.secret
    ]
    app_engine_default = [
      "dagster-database-password",
      data.google_secret_manager_secret_version.oauth_cookie_secret.secret,
      data.google_secret_manager_secret_version.github_dagster_client_id.secret,
      data.google_secret_manager_secret_version.github_dagster_client_secret.secret,
      data.google_secret_manager_secret_version.github_guard_token.secret
    ]
  }

  iam_service_account_impersonation = {
    runtime = {
      service_account = google_service_account.cloud_run_runtime.name
      members = [
        local.iam_service_accounts.cloud_build,
        local.iam_service_accounts.runtime
      ]
    }
    compute_default = {
      service_account = data.google_compute_default_service_account.compute_default.name
      members = [
        local.iam_service_accounts.cloud_build,
        local.iam_service_accounts.runtime
      ]
    }
    app_engine_default = {
      service_account = data.google_app_engine_default_service_account.app_engine.name
      members = [
        local.iam_service_accounts.runtime
      ]
    }
  }
}

resource "google_project_iam_member" "service_account_project_roles" {
  for_each = {
    for binding in flatten([
      for account, roles in local.iam_project_roles : [
        for role in roles : {
          key     = "${account}-${replace(role, "/", "_")}"
          account = account
          role    = role
        }
      ]
    ]) : binding.key => binding
  }

  project = data.google_project.current.project_id
  role    = each.value.role
  member  = "serviceAccount:${local.iam_service_accounts[each.value.account]}"
}

resource "google_service_account_iam_member" "service_account_impersonation" {
  for_each = {
    for binding in flatten([
      for account, config in local.iam_service_account_impersonation : [
        for idx, member in config.members : {
          key             = "${account}-${idx}"
          service_account = config.service_account
          member          = member
        }
      ]
    ]) : binding.key => binding
  }

  service_account_id = each.value.service_account
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${each.value.member}"
}

resource "google_secret_manager_secret_iam_member" "service_account_secret_access" {
  for_each = {
    for binding in flatten([
      for account, secrets in local.iam_secret_access : [
        for index, secret in secrets : {
          key     = "${account}-${index}"
          account = account
          secret  = secret
        }
      ]
    ]) : binding.key => binding
  }

  project    = data.google_project.current.project_id
  secret_id  = each.value.secret
  role       = "roles/secretmanager.secretAccessor"
  member     = "serviceAccount:${local.iam_service_accounts[each.value.account]}"
  depends_on = [google_project_service.required["secretmanager.googleapis.com"]]
}

resource "google_artifact_registry_repository_iam_member" "compute_default_reader" {
  project    = data.google_project.current.project_id
  location   = google_artifact_registry_repository.containers.location
  repository = google_artifact_registry_repository.containers.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${local.iam_service_accounts.compute_default}"

  depends_on = [
    google_project_service.required["artifactregistry.googleapis.com"],
    google_project_iam_member.service_account_project_roles["compute_default-roles_artifactregistry.reader"]
  ]
}

resource "google_project_iam_member" "engine_run_jobs" {
  project = data.google_project.current.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${data.google_compute_default_service_account.compute_default.email}"
}

resource "google_service_account_iam_member" "allow_impersonation" {
  service_account_id = google_service_account.cloud_run_runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${data.google_compute_default_service_account.compute_default.email}"
}

resource "google_cloud_run_v2_job" "job" {
  name     = "pipelines-job"
  location = local.region

  deletion_protection = false

  template {
    template {
      containers {
        name  = "job"
        image = local.pipelines_image

        command = ["/bin/sh", "-c"]
        args = [
          "dagster api execute_run --run-id \"$DAGSTER_RUN_ID\""
        ]

        env {
          name  = "DAGSTER_HOME"
          value = "/etc/dagster"
        }

        env {
          name  = "DATABASE_HOST"
          value = "127.0.0.1"
        }
        env {
          name  = "DATABASE_PORT"
          value = "5432"
        }
        env {
          name  = "DATABASE_SSLMODE"
          value = "disable"
        }
        env {
          name  = "DATABASE_USER"
          value = google_sql_user.pipelines_user.name
        }
        env {
          name = "DATABASE_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = data.google_secret_manager_secret_version.pipelines_database_password.secret
              version = "latest"
            }
          }
        }
        env {
          name  = "DATABASE_NAME"
          value = google_sql_database.pipelines_meta.name
        }

        env {
          name  = "DAGSTER_RUN_ID"
          value = "dummy"
        }
      }

      service_account = google_service_account.cloud_run_runtime.email
      timeout         = "3600s"
    }
  }

  launch_stage = "GA"

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image
    ]
  }
}

resource "google_compute_address" "engine" {
  name   = "engine"
  region = local.region
}

resource "google_compute_resource_policy" "engine_stop" {
  name   = "engine-stop-4am"
  region = local.region

  instance_schedule_policy {
    time_zone = "Europe/Berlin"

    vm_stop_schedule {
      schedule = "0 4 * * *"
    }
  }
}

resource "google_compute_instance" "engine" {
  name         = "engine"
  machine_type = "e2-medium"
  zone         = local.zone

  tags = ["pipelines","evaluation"]
  labels = {
    role      = "computation"
    workload  = "engine"
    component = "engine"
  }

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2204-lts"
      size  = 50
      type  = "pd-standard"
    }
  }

  network_interface {
    network = "default"
    access_config {
      nat_ip = google_compute_address.engine.address
    }
  }

  service_account {
    email  = local.compute_default_service_account
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  metadata = {
    "google-logging-enabled"    = "TRUE"
    "google-monitoring-enabled" = "TRUE"
    "enable-osconfig"           = "TRUE"
    "unit-version"              = local.unit_version
      "user-data" = templatefile("${path.module}/computation/initialize.yaml", {
        pipelines_image_repository  = local.pipelines_image_repository
        pipelines_code_image_repository = local.pipelines_code_image_repository
        evaluation_image_repository = local.evaluation_image_repository
        default_image_tag           = local.default_image_tag
        unit_version                = local.unit_version
        artifact_registry_host      = local.artifact_registry_host
        project_id                  = data.google_project.current.project_id
        region                      = local.region
        database_host               = google_sql_database_instance.database.public_ip_address
        database_port               = 5432
        database_sslmode            = "require"
        database_user               = google_sql_user.pipelines_user.name
        database_password           = google_sql_user.pipelines_user.password
        database_name               = google_sql_database.pipelines_meta.name
        gcs_bucket                  = google_storage_bucket.bucket.name
        argilla_github_client_id    = data.google_secret_manager_secret_version.github_argilla_client_id.secret_data
        argilla_github_client_secret = data.google_secret_manager_secret_version.github_argilla_client_secret.secret_data
        github_oauth_callback_url   = var.evaluation_github_callback_url
        branches_port_start         = 4100
        branches_port_end           = 4109
      })
    }
  resource_policies = [
    google_compute_resource_policy.engine_stop.id
  ]

  depends_on = [
    google_project_service.required["compute.googleapis.com"],
    google_project_service.required["osconfig.googleapis.com"],
    google_project_iam_member.service_account_project_roles["compute_default-roles_artifactregistry.reader"]
  ]

  lifecycle {
    replace_triggered_by = [
      google_sql_database_instance.database,
      null_resource.database_credentials_hash,
    ]
  }
}
