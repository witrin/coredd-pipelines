#cloud-config
package_update: true
package_upgrade: false

write_files:
  - path: /etc/apt/apt.conf.d/99locktimeout
    permissions: '0644'
    content: |
      DPkg::Lock::Timeout "1200";

  - path: /usr/local/bin/provision.sh
    permissions: '0755'
    content: |
      #!/usr/bin/env bash
      set -euo pipefail

      # Google Cloud SDK Repository
      curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        | gpg --batch --yes --dearmor >/usr/share/keyrings/cloud.google.gpg

      echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        >/etc/apt/sources.list.d/google-cloud-sdk.list

      # Artifact Registry Transport
      echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] http://packages.cloud.google.com/apt apt-transport-artifact-registry-stable main" \
        >/etc/apt/sources.list.d/artifact-registry-transport.list

      apt-get update -y
      apt-get install -y apt-transport-artifact-registry curl gnupg

      # Custom Artifact Registry Repository
      curl -fsSL "https://${region}-apt.pkg.dev/doc/repo-signing-key.gpg" \
        | gpg --batch --yes --dearmor >/usr/share/keyrings/artifact-registry.gpg

      echo "deb [arch=all signed-by=/usr/share/keyrings/artifact-registry.gpg] ar+https://${region}-apt.pkg.dev/projects/${project} ${packages} main" \
        >/etc/apt/sources.list.d/${packages}.list

      apt-get update -y

      # Docker
      curl -fsSL https://get.docker.com | sh
      systemctl enable docker.service

      # Docker Credentials
      install -d -m 700 /root/.docker
      cat >/root/.docker/config.json <<'DOCKER'
      {
        "credHelpers": {
          "${registry}": "gcloud"
        }
      }
      DOCKER
      chmod 600 /root/.docker/config.json

      # OS Config Agent STARTEN (ist vorinstalliert)
      systemctl enable --now google-osconfig-agent

runcmd:
  - [bash, /usr/local/bin/provision.sh]
