resource "google_service_account" "runtime" {
  account_id   = "pipelines-runtime"
  display_name = "Pipelines Cloud Run"
}

resource "google_artifact_registry_repository" "containers" {
  location      = local.region
  repository_id = "containers"
  format        = "DOCKER"
  description   = "Container images for the project."
  depends_on    = [google_project_service.required["artifactregistry.googleapis.com"]]
}

resource "google_artifact_registry_repository" "apt" {
  location      = local.region
  repository_id = "apt"
  format        = "APT"
  description   = "Host packages for compute instances."
  depends_on    = [google_project_service.required["artifactregistry.googleapis.com"]]
}

resource "google_cloudbuild_trigger" "deploy" {
  name        = "computation"
  description = "Builds and deploys the pipeline computation when new commits are pushed."
  location    = local.region
  filename    = ".cloud/computation/delivery.yaml"

  repository_event_config {
    repository = local.cloud_build_repository_resource
    push {
      branch = "^main$"
    }
  }

  included_files = [
    ".argilla/**",
    ".dagster/**",
    ".cloud/**",
    "Dockerfile",
    "pyproject.toml",
    "requirements.lock"
  ]

  substitutions = {
    "_REGION"   = local.region
    "_IMAGES"   = google_artifact_registry_repository.containers.repository_id
    "_PACKAGES" = google_artifact_registry_repository.apt.repository_id
    "_IMAGE"    = "pipelines"
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

locals {
  iam_service_accounts = {
    runtime            = google_service_account.runtime.email
    build              = local.build_service_account
    compute_default    = local.compute_default_service_account
    app_engine_default = local.app_engine_default_service_account
  }

  iam_project_roles = {
    runtime = [
      "roles/artifactregistry.reader",
      "roles/bigquery.jobUser",
      "roles/cloudsql.client",
      "roles/logging.logWriter"
    ]
    build = [
      "roles/artifactregistry.writer",
      "roles/cloudfunctions.developer",
      "roles/logging.logWriter",
      "roles/run.admin",
      "roles/osconfig.osPolicyAssignmentAdmin",
      "roles/compute.instanceAdmin.v1"
    ]
    compute_default = [
      "roles/artifactregistry.reader",
      "roles/logging.logWriter"
    ]
  }

  iam_secret_access = {
    runtime = [
      data.google_secret_manager_secret_version.pipelines_database_password.secret,
      data.google_secret_manager_secret_version.oauth_cookie_secret.secret,
      data.google_secret_manager_secret_version.github_dagster_client_id.secret,
      data.google_secret_manager_secret_version.github_dagster_client_secret.secret,
      data.google_secret_manager_secret_version.github_guard_token.secret
    ]
    compute_default = [
      data.google_secret_manager_secret_version.pipelines_database_password.secret,
      data.google_secret_manager_secret_version.oauth_cookie_secret.secret,
      data.google_secret_manager_secret_version.github_dagster_client_id.secret,
      data.google_secret_manager_secret_version.github_dagster_client_secret.secret,
      data.google_secret_manager_secret_version.github_argilla_client_id.secret,
      data.google_secret_manager_secret_version.github_argilla_client_secret.secret,
      data.google_secret_manager_secret_version.github_guard_token.secret
    ]
    app_engine_default = [
      data.google_secret_manager_secret_version.pipelines_database_password.secret,
      data.google_secret_manager_secret_version.oauth_cookie_secret.secret,
      data.google_secret_manager_secret_version.github_dagster_client_id.secret,
      data.google_secret_manager_secret_version.github_dagster_client_secret.secret,
      data.google_secret_manager_secret_version.github_guard_token.secret
    ]
  }

  iam_service_account_impersonation = {
    runtime = {
      service_account = google_service_account.runtime.name
      members = [
        local.iam_service_accounts.build,
        local.iam_service_accounts.runtime
      ]
    }
    compute_default = {
      service_account = data.google_compute_default_service_account.compute_default.name
      members = [
        local.iam_service_accounts.build,
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

resource "google_service_account_iam_member" "app_engine_default_build_act_as" {
  service_account_id = data.google_app_engine_default_service_account.app_engine.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.build_service_account}"
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
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${data.google_compute_default_service_account.compute_default.email}"
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
    "unit-version"              = local.unit_version
    "user-data" = templatefile("${path.module}/computation/initialize.yaml.tpl", {
      region   = local.region
      project  = data.google_project.current.project_id
      packages = google_artifact_registry_repository.apt.repository_id
      registry = local.artifact_registry_host
    })
    "docker" = base64encode(jsonencode({
      registry = local.artifact_registry_host
    }))
    "dagster" = base64encode(jsonencode({
      project = data.google_project.current.project_id
      region  = local.region
      bucket  = google_storage_bucket.bucket.name
      account = google_service_account.runtime.email
      database = {
        host       = google_sql_database_instance.database.public_ip_address
        port       = 5432
        name       = google_sql_database.pipelines_meta.name
        user       = google_sql_user.pipelines_user.name
        connection = google_sql_database_instance.database.connection_name
        ssl        = "require"
      }
      ports = {
        start = 4100
        end   = 4109
      }
      images = {
        pipelines = local.pipelines_image_repository
        code      = local.code_image_repository
        job       = local.job_image_repository
      }
      grpc = {
        port = 4100
      }
      prefix = "pipelines"
    }))
    "argilla" = base64encode(jsonencode({
      project = data.google_project.current.project_id
      region  = local.region
      database = {
        host    = google_sql_database_instance.database.public_ip_address
        port    = 5432
        name    = "argilla"
        user    = google_sql_user.pipelines_user.name
        ssl     = "require"
      }
      image = local.evaluation_image_repository
    }))
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
