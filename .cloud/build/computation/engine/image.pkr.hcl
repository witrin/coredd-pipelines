packer {
  required_plugins {
    googlecompute = {
      source  = "github.com/hashicorp/googlecompute"
      version = ">= 1.0.0"
    }
  }
}

variable "project" {
  type = string
}

variable "zone" {
  type = string
}

variable "image_name" {
  type = string
}

variable "image_family" {
  type = string
}

variable "region" {
  type = string
}

variable "packages" {
  type = string
}

variable "registry" {
  type = string
}

source "googlecompute" "engine" {
  project_id          = var.project
  zone                = var.zone
  source_image_family = "ubuntu-2204-lts"
  image_name          = var.image_name
  image_family        = var.image_family
  machine_type        = "e2-medium"
  disk_size           = 50
  tags                = ["pipelines", "evaluation"]
  ssh_username        = "packer"
  scopes              = ["https://www.googleapis.com/auth/cloud-platform"]
}

build {
  name    = "engine"
  sources = ["source.googlecompute.engine"]

  provisioner "shell" {
    execute_command = "sudo -S /bin/bash -c '{{ .Vars }} {{ .Path }}'"
    inline = [
<<EOC
cat >/tmp/provision.sh <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
  | gpg --batch --yes --dearmor >/usr/share/keyrings/cloud.google.gpg

echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
  >/etc/apt/sources.list.d/google-cloud-sdk.list

echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] http://packages.cloud.google.com/apt apt-transport-artifact-registry-stable main" \
  >/etc/apt/sources.list.d/artifact-registry-transport.list

rm -rf /var/lib/apt/lists/*
apt update -y
apt install -y apt-transport-artifact-registry google-cloud-cli curl gnupg

curl -fsSL "https://${var.region}-apt.pkg.dev/doc/repo-signing-key.gpg" \
  | gpg --batch --yes --dearmor >/usr/share/keyrings/artifact-registry.gpg

echo "deb [arch=all signed-by=/usr/share/keyrings/artifact-registry.gpg] ar+https://${var.region}-apt.pkg.dev/projects/${var.project} apt main" \
  >/etc/apt/sources.list.d/${var.packages}.list

apt update -y

apt install -y docker.io
systemctl enable docker.service

install -d -m 700 /root/.docker
cat >/root/.docker/config.json <<'DOCKER'
{
  "credHelpers": {
    "${var.registry}": "gcloud"
  }
}
DOCKER
chmod 600 /root/.docker/config.json
SCRIPT
EOC
      ,
      "chmod +x /tmp/provision.sh",
      "bash /tmp/provision.sh"
    ]
  }
}
