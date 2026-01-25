resource "google_compute_firewall" "allow_engine_services" {
  name    = "allow-engine-services"
  network = "default"

  direction     = "INGRESS"
  source_ranges = ["130.211.0.0/22", "35.191.0.0/16"]

  allow {
    protocol = "tcp"
    ports    = ["3000", "6900"]
  }

  target_tags = ["pipelines", "evaluation"]
}

resource "google_compute_firewall" "deny_internal_pipelines" {
  name    = "deny-internal-to-pipelines"
  network = "default"

  direction = "INGRESS"
  priority  = 900

  source_ranges = ["10.128.0.0/9"]
  target_tags   = ["pipelines"]

  deny {
    protocol = "tcp"
    ports    = ["3000", "6900"]
  }
}

resource "google_compute_instance_group" "engine_group" {
  name = "engine-group"
  zone = local.zone

  named_port {
    name = "pipelines"
    port = 3000
  }

  named_port {
    name = "evaluation"
    port = 6900
  }
}

resource "google_compute_instance_group_membership" "engine_group_membership" {
  zone           = local.zone
  instance_group = google_compute_instance_group.engine_group.name
  instance       = google_compute_instance.engine.self_link
}

resource "google_compute_health_check" "pipelines_health" {
  name = "pipelines"

  http_health_check {
    port         = 3000
    request_path = "/"
  }

  check_interval_sec  = 10
  timeout_sec         = 5
  healthy_threshold   = 2
  unhealthy_threshold = 3
}

resource "google_compute_health_check" "evaluation_health" {
  name = "evaluation"

  http_health_check {
    port         = 6900
    request_path = "/"
  }

  check_interval_sec  = 10
  timeout_sec         = 5
  healthy_threshold   = 2
  unhealthy_threshold = 3
}

resource "google_compute_backend_service" "pipelines_service" {
  name                        = "pipelines-service"
  protocol                    = "HTTP"
  port_name                   = "pipelines"
  timeout_sec                 = 30
  connection_draining_timeout_sec = 0

  health_checks = [
    google_compute_health_check.pipelines_health.self_link
  ]

  backend {
    group = google_compute_instance_group.engine_group.self_link
  }
}

resource "google_compute_backend_service" "evaluation_service" {
  name                        = "evaluation-service"
  protocol                    = "HTTP"
  port_name                   = "evaluation"
  timeout_sec                 = 30
  connection_draining_timeout_sec = 0

  health_checks = [
    google_compute_health_check.evaluation_health.self_link
  ]

  backend {
    group = google_compute_instance_group.engine_group.self_link
  }
}

resource "google_compute_url_map" "engine_url_map" {
  name            = "engine-url-map"
  default_service = google_compute_backend_service.pipelines_service.self_link

  host_rule {
    hosts        = ["evaluation.bug-request.dev"]
    path_matcher = "evaluation"
  }

  path_matcher {
    name            = "evaluation"
    default_service = google_compute_backend_service.evaluation_service.self_link
  }
}

resource "google_compute_managed_ssl_certificate" "engine_certificate" {
  name = "engine-certificate"

  managed {
    domains = [
      "pipelines.bug-request.dev",
      "evaluation.bug-request.dev"
    ]
  }

  lifecycle {
    ignore_changes = [managed]
  }
}

resource "google_compute_target_https_proxy" "engine_https_proxy" {
  name    = "engine-https-proxy"
  url_map = google_compute_url_map.engine_url_map.self_link
  ssl_certificates = [
    google_compute_managed_ssl_certificate.engine_certificate.self_link
  ]
}

resource "google_compute_global_address" "engine_load_balancer" {
  name = "engine-load-balancer"
}

resource "google_compute_global_forwarding_rule" "engine_https" {
  name                  = "engine-https-forwarding-rule"
  target                = google_compute_target_https_proxy.engine_https_proxy.self_link
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL"
  ip_protocol           = "TCP"
  ip_address            = google_compute_global_address.engine_load_balancer.address
}

resource "google_dns_managed_zone" "pipelines" {
  name        = "pipelines-zone"
  dns_name    = "pipelines.bug-request.dev."
  description = "Managed zone for pipelines.bug-request.dev"
}

resource "google_dns_record_set" "pipelines_a" {
  name         = "pipelines.bug-request.dev."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.pipelines.name
  rrdatas      = [google_compute_global_address.engine_load_balancer.address]
}

resource "google_dns_managed_zone" "evaluation" {
  name        = "evaluation-zone"
  dns_name    = "evaluation.bug-request.dev."
  description = "Managed zone for evaluation.bug-request.dev"
}

resource "google_dns_record_set" "evaluation_a" {
  name         = "evaluation.bug-request.dev."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.evaluation.name
  rrdatas      = [google_compute_global_address.engine_load_balancer.address]
}
