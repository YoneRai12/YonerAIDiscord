#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  printf '%s\n' "root_required" >&2
  exit 64
fi
if [[ "$(cat /etc/hostname)" != "yonerai-search-sandbox" ]]; then
  printf '%s\n' "guest_identity_mismatch" >&2
  exit 65
fi
if [[ ! "${YONERAI_SOURCE_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]]; then
  printf '%s\n' "source_commit_invalid" >&2
  exit 66
fi

stage_root="/var/tmp/yonerai-search-stage/${YONERAI_SOURCE_COMMIT}"
release_root="/opt/yonerai-search/releases/${YONERAI_SOURCE_COMMIT}"
service_user="yonerai-search"

test -f "${stage_root}/infra/search-sandbox/VERSION.lock"
test -f "${stage_root}/infra/search-sandbox/LICENSES.md"
test -f "${stage_root}/infra/search-sandbox/compose.yaml"
test -f "${stage_root}/tools/hyperv-search-sandbox/guest/compose.hyperv.yaml"
test -d "${stage_root}/src/yonerai_discord"

. /etc/os-release
test "${ID}" = "ubuntu"
test "${VERSION_ID}" = "24.04"

export DEBIAN_FRONTEND=noninteractive
/usr/bin/apt-get update
/usr/bin/apt-get install --yes --no-install-recommends \
  ca-certificates \
  curl \
  podman \
  podman-compose \
  uidmap

if ! /usr/bin/getent passwd "${service_user}" >/dev/null; then
  /usr/sbin/useradd \
    --create-home \
    --home-dir /var/lib/yonerai-search \
    --shell /usr/sbin/nologin \
    "${service_user}"
fi
service_uid="$(/usr/bin/id -u "${service_user}")"

/usr/bin/install -d -o root -g "${service_user}" -m 0750 \
  /opt/yonerai-search \
  /opt/yonerai-search/releases
if [[ -e "${release_root}" ]]; then
  printf '%s\n' "release_already_exists" >&2
  exit 67
fi
/bin/cp -a "${stage_root}" "${release_root}"
/bin/chown -R root:"${service_user}" "${release_root}"
/usr/bin/find "${release_root}" -type d -exec /bin/chmod 0750 {} +
/usr/bin/find "${release_root}" -type f -exec /bin/chmod 0640 {} +
/bin/ln -sfn "${release_root}" /opt/yonerai-search/current

/usr/bin/install -d -o "${service_user}" -g "${service_user}" -m 0700 \
  /var/lib/yonerai-search \
  /var/lib/yonerai-search/run

/usr/bin/install -o root -g root -m 0644 /dev/stdin \
  /etc/systemd/system/yonerai-search-sandbox.service <<EOF
[Unit]
Description=YonerAI private Search Sandbox
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${service_user}
Group=${service_user}
Environment=HOME=/var/lib/yonerai-search
Environment=XDG_RUNTIME_DIR=/var/lib/yonerai-search/run
WorkingDirectory=/opt/yonerai-search/current/infra/search-sandbox
ExecStart=/usr/bin/podman-compose -f compose.yaml -f /opt/yonerai-search/current/tools/hyperv-search-sandbox/guest/compose.hyperv.yaml up --build --remove-orphans
ExecStop=/usr/bin/podman-compose -f compose.yaml -f /opt/yonerai-search/current/tools/hyperv-search-sandbox/guest/compose.hyperv.yaml down --timeout 10
TimeoutStartSec=900
TimeoutStopSec=30
Restart=no
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/yonerai-search
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
EOF

/usr/bin/systemctl daemon-reload
/usr/bin/systemctl enable --now yonerai-search-sandbox.service

deadline="$((SECONDS + 180))"
while (( SECONDS < deadline )); do
  if /usr/bin/curl \
      --fail \
      --silent \
      --show-error \
      --noproxy "*" \
      --max-time 3 \
      http://127.0.0.1:8787/healthz >/dev/null; then
    break
  fi
  /usr/bin/sleep 2
done
/usr/bin/curl \
  --fail \
  --silent \
  --show-error \
  --noproxy "*" \
  --max-time 3 \
  http://127.0.0.1:8787/healthz >/dev/null

/usr/bin/python3 - <<'PY'
import json
import subprocess

versions = {
    "schema": "yonerai.search-sandbox.guest-provenance.v1",
    "podman": subprocess.check_output(
        ["/usr/bin/podman", "--version"], text=True, timeout=5
    ).strip(),
    "podman_compose": subprocess.check_output(
        ["/usr/bin/podman-compose", "--version"], text=True, timeout=5
    ).strip(),
}
with open(
    "/var/lib/yonerai-search/provenance.json",
    "w",
    encoding="utf-8",
) as handle:
    json.dump(versions, handle, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    handle.write("\n")
PY
/bin/chown "${service_user}:${service_user}" /var/lib/yonerai-search/provenance.json
/bin/chmod 0600 /var/lib/yonerai-search/provenance.json

printf '%s\n' "yonerai_search_guest_ready"
