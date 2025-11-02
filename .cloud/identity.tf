resource "google_project_service_identity" "developer_connect" {
  provider = google-beta
  project  = data.google_project.current.project_id
  service  = "developerconnect.googleapis.com"

  depends_on = [google_project_service.required["developerconnect.googleapis.com"]]
}

resource "google_project_iam_member" "developer_connect_cloudbuild_service_agent" {
  project = data.google_project.current.project_id
  role    = "roles/cloudbuild.serviceAgent"
  member  = "serviceAccount:${local.developer_connect_service_account}"
}

resource "google_cloudbuild_trigger" "identity" {
  name        = "identity"
  description = "Deploys the signup blocking function when new commits are pushed."
  location    = local.region
  filename    = ".cloud/identity/delivery.yaml"

  repository_event_config {
    repository = local.cloud_build_repository_resource
    push {
      branch = "^main$"
    }
  }

  substitutions = {
    "_REGION"     = local.region
    "_GITHUB_ORG" = var.github_org
    "_BUILD_BUCKET" = google_storage_bucket.build.name
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

data "archive_file" "signup_dummy" {
  type        = "zip"
  output_path = "${path.module}/identity/signup-dummy.zip"

  source {
    filename = "index.js"
    content  = <<-EOT
      exports.handler = (req, res) => {
        res.status(200).send("Signup placeholder function.");
      };
    EOT
  }

  source {
    filename = "package.json"
    content  = <<-EOT
      {
        "name": "signup-dummy",
        "version": "1.0.0"
      }
    EOT
  }
}

resource "google_storage_bucket_object" "signup_dummy" {
  name   = "functions/signup.zip"
  bucket = google_storage_bucket.build.name
  source = data.archive_file.signup_dummy.output_path

  lifecycle {
    ignore_changes = [
      content,
      source
    ]
  }
}

resource "google_cloudfunctions_function" "signup" {
  name         = "signup"
  description  = "Placeholder signup blocking hook managed through Terraform."
  runtime      = "nodejs22"
  entry_point  = "handler"
  region       = local.region
  trigger_http = true

  source_archive_bucket = google_storage_bucket.build.name
  source_archive_object = "functions/signup.zip"

  environment_variables = {
    GITHUB_ORG = var.github_org
  }

  depends_on = [google_project_service.required["cloudfunctions.googleapis.com"]]

  lifecycle {
    ignore_changes = [
      source_archive_bucket,
      source_archive_object
    ]
  }
}

resource "google_cloudfunctions_function_iam_member" "signup_invoker" {
  region         = local.region
  project        = data.google_project.current.project_id
  cloud_function = google_cloudfunctions_function.signup.name
  role           = "roles/cloudfunctions.invoker"
  member         = "allUsers"
}

resource "google_identity_platform_config" "project" {
  depends_on = [google_project_service.required["identitytoolkit.googleapis.com"]]
  multi_tenant {
    allow_tenants = true
  }
  blocking_functions {
    triggers {
      event_type = "beforeCreate"
      function_uri = google_cloudfunctions_function.signup.https_trigger_url
    }
  }
}

# Enable GitHub as a provider in Identity Platform (project-level config used by IAP GCIP tenant "_${local.project_number}")
resource "google_identity_platform_default_supported_idp_config" "github" {
  idp_id        = "github.com"
  enabled       = true
  client_id     = data.google_secret_manager_secret_version.github_dagster_client_id.secret_data
  client_secret = data.google_secret_manager_secret_version.github_dagster_client_secret.secret_data
  depends_on    = [google_project_service.required["identitytoolkit.googleapis.com"]]
}

resource "google_cloud_run_v2_service" "engine_authentication" {
  name                = "engine-authentication"
  location            = local.region
  deletion_protection = false

  template {
    containers {
      image = "gcr.io/gcip-iap/authui"
      resources {
        limits = {
          memory = "256Mi"
        }
        cpu_idle = true
      }
      env {
        name  = "API_KEY"
        value = google_identity_platform_config.project.client[0].api_key
      }
      env {
        name  = "AUTH_DOMAIN"
        value = "auth.bug-request.dev"
      }
      env {
        name  = "TENANT_ID"
        value = "_${local.project_number}"
      }
      env {
        name  = "PROVIDERS"
        value = "github.com"
      }
    }
    service_account = local.compute_default_service_account
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    timeout = "30s"
  }

  lifecycle {
    ignore_changes = [annotations, labels, ingress, traffic, template[0].containers[0].image]
  }
}

resource "google_cloud_run_v2_service_iam_member" "engine_authentication" {
  project  = data.google_project.current.project_id
  location = local.region
  name     = google_cloud_run_v2_service.engine_authentication.name

  role   = "roles/run.invoker"
  member = "allUsers"
}

resource "google_iap_settings" "engine" {
  name = local.iap_backend_path
  access_settings {
    gcip_settings {
      login_page_uri = "https://auth.bug-request.dev?apiKey=${google_identity_platform_config.project.client[0].api_key}"
      tenant_ids     = ["_${local.project_number}"]
    }
  }
}

resource "google_iap_web_backend_service_iam_member" "access_user" {
  web_backend_service = local.iap_backend_path
  role                = "roles/iap.httpsResourceAccessor"
  member              = "allAuthenticatedUsers"
}

resource "google_cloud_run_domain_mapping" "engine_authentication" {
  location = local.region
  name     = "auth.bug-request.dev"

  metadata {
    namespace = data.google_project.current.project_id
  }
  spec {
    route_name = google_cloud_run_v2_service.engine_authentication.name
  }
}
