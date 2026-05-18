resource "google_service_account" "runtime" {
  account_id   = "pipelines-runtime"
  display_name = "Pipelines Cloud Run"
}

locals {
  accounts = {
    runtime            = google_service_account.runtime.email
    compute_default    = local.compute_default_service_account
    app_engine_default = local.app_engine_default_service_account
  }

  roles = {
    runtime = [
      "roles/artifactregistry.reader",
      "roles/bigquery.jobUser",
      "roles/cloudsql.client",
      "roles/logging.logWriter"
    ]
    compute_default = [
      "roles/artifactregistry.reader",
      "roles/logging.logWriter"
    ]
  }

  secrets = {
    runtime = [
      data.google_secret_manager_secret_version.pipelines_database_password.secret,
      data.google_secret_manager_secret_version.oauth_cookie_secret.secret,
      data.google_secret_manager_secret_version.github_dagster_client_id.secret,
      data.google_secret_manager_secret_version.github_dagster_client_secret.secret,
      data.google_secret_manager_secret_version.github_guard_token.secret,
      data.google_secret_manager_secret_version.github_dagster_api_secret.secret,
      data.google_secret_manager_secret_version.together_ai_api_key.secret
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

  impersonation = {
    runtime = {
      service_account = google_service_account.runtime.name
      members = [
        local.build_service_account,
        local.accounts.runtime
      ]
    }
    compute_default = {
      service_account = data.google_compute_default_service_account.compute_default.name
      members = [
        local.build_service_account,
        local.accounts.runtime
      ]
    }
    app_engine_default = {
      service_account = data.google_app_engine_default_service_account.app_engine.name
      members = [
        local.accounts.runtime
      ]
    }
  }
}

resource "google_project_iam_member" "roles" {
  for_each = {
    for binding in flatten([
      for account, roles in local.roles : [
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
  member  = "serviceAccount:${local.accounts[each.value.account]}"
}

resource "google_service_account_iam_member" "impersonation" {
  for_each = {
    for binding in flatten([
      for account, config in local.impersonation : [
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

resource "google_secret_manager_secret_iam_member" "secrets" {
  for_each = {
    for binding in flatten([
      for account, secrets in local.secrets : [
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
  member     = "serviceAccount:${local.accounts[each.value.account]}"
}

resource "google_artifact_registry_repository_iam_member" "compute_default_reader" {
  project    = data.google_project.current.project_id
  location   = local.region
  repository = "containers"
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${local.accounts.compute_default}"

  depends_on = [
    google_project_iam_member.roles["compute_default-roles_artifactregistry.reader"]
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

resource "google_project_iam_member" "runtime_secrets" {
  project = data.google_project.current.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${local.accounts.runtime}"

  condition {
    title       = "secrets_prefix_only"
    description = "Access only to secrets with ${local.secrets_prefix}- prefix"
    expression  = "resource.name.startsWith(\"projects/${local.project_number}/secrets/${local.secrets_prefix}-\")"
  }
}

resource "google_compute_address" "engine" {
  name   = "engine"
  region = local.region
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
      image = data.terraform_remote_state.build.outputs.computation.image
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
    "enable-osconfig"           = "TRUE"
    "google-logging-enabled"    = "TRUE"
    "google-monitoring-enabled" = "TRUE"
    "unit-version"              = local.unit_version
    "user-data" = templatefile("${path.module}/../build/computation/engine/initialize.yaml.tpl", {
      region   = local.region
      project  = data.google_project.current.project_id
      packages = data.terraform_remote_state.build.outputs.registry.packages
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
      prefix = local.secrets_prefix
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
  depends_on = [
    google_project_iam_member.roles["compute_default-roles_artifactregistry.reader"]
  ]

  lifecycle {
    replace_triggered_by = [
      null_resource.database_credentials_hash,
    ]
  }
}
