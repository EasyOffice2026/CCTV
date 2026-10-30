#!/usr/bin/env bash
# Provision a Jetson (JetPack 6 / Ubuntu 22.04) as a CCTV edge box. Safe to run again.
#
#   sudo ./provision.sh
#
# Optional environment variables:
#   CCTV_EDGE_CLOUD_URL    cloud API URL (prompted if unset)
#   CCTV_EDGE_DEVICE_TOKEN device token from the admin panel (prompted if unset, not echoed)
#   CCTV_EDGE_IMAGE        edge image to run (default: see docker-compose.yml)
#   NVME_DEVICE            e.g. /dev/nvme0n1, mounted at /var/lib/cctv-edge for footage
#   FORMAT_NVME=yes        format NVME_DEVICE as ext4 if it has no filesystem yet
#   TAILSCALE_AUTHKEY      join the support VPN (tailnet) for remote maintenance
#   ALLOW_NON_JETSON=yes   allow running on a non-Jetson Ubuntu box (lab/testing)
#
# Camera passwords go in /etc/cctv-edge/credentials.json afterwards (see edge/README.md).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_UID=10001                     # the service user inside the edge image
CONF_DIR=/etc/cctv-edge
DATA_DIR=/var/lib/cctv-edge
INSTALL_DIR=/opt/cctv-edge

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"

log "Checking platform"
if [[ -f /etc/nv_tegra_release ]]; then
    head -1 /etc/nv_tegra_release
elif [[ ${ALLOW_NON_JETSON:-} != yes ]]; then
    die "not a Jetson (no /etc/nv_tegra_release); set ALLOW_NON_JETSON=yes to continue"
fi

log "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q docker.io docker-compose-v2 chrony curl jq
if apt-cache show nvidia-container-toolkit >/dev/null 2>&1; then
    apt-get install -y -q nvidia-container-toolkit
    nvidia-ctk runtime configure --runtime=docker
fi
systemctl enable --now docker chrony

log "Clock: NTP on (event times come from this box, never from the NVR)"
timedatectl set-ntp true || true

if [[ -f /etc/nv_tegra_release ]]; then
    log "Performance: max power mode, no desktop"
    nvpmodel -m 0 || true
    systemctl set-default multi-user.target
fi

log "Hardware watchdog: reboot if the kernel hangs"
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/cctv-watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=30s
RebootWatchdogSec=5min
EOF
systemctl daemon-reexec

mkdir -p "$DATA_DIR"
if [[ -n ${NVME_DEVICE:-} ]]; then
    log "Storage: $NVME_DEVICE -> $DATA_DIR"
    [[ -b $NVME_DEVICE ]] || die "$NVME_DEVICE is not a block device"
    if ! blkid "$NVME_DEVICE" >/dev/null 2>&1; then
        [[ ${FORMAT_NVME:-} == yes ]] || die "$NVME_DEVICE has no filesystem; set FORMAT_NVME=yes to format it"
        mkfs.ext4 -L cctv-data "$NVME_DEVICE"
    fi
    uuid="$(blkid -s UUID -o value "$NVME_DEVICE")"
    if ! grep -q "$uuid" /etc/fstab; then
        echo "UUID=$uuid $DATA_DIR ext4 defaults,noatime,nofail 0 2" >> /etc/fstab
    fi
    systemctl daemon-reload
    mountpoint -q "$DATA_DIR" || mount "$DATA_DIR"
fi
chown "$APP_UID:$APP_UID" "$DATA_DIR"

log "Configuration in $CONF_DIR"
install -d -m 700 "$CONF_DIR"
if [[ ! -f $CONF_DIR/edge.env ]]; then
    cloud_url="${CCTV_EDGE_CLOUD_URL:-}"
    token="${CCTV_EDGE_DEVICE_TOKEN:-}"
    [[ -n $cloud_url ]] || read -rp "Cloud URL: " cloud_url
    [[ -n $token ]] || { read -rsp "Device token: " token; echo; }
    [[ -n $cloud_url && -n $token ]] || die "cloud URL and device token are required"
    umask 077
    cat > "$CONF_DIR/edge.env" <<EOF
CCTV_EDGE_CLOUD_URL=$cloud_url
CCTV_EDGE_DEVICE_TOKEN=$token
EOF
    [[ -z ${CCTV_EDGE_IMAGE:-} ]] || echo "CCTV_EDGE_IMAGE=$CCTV_EDGE_IMAGE" >> "$CONF_DIR/edge.env"
fi
[[ -f $CONF_DIR/credentials.json ]] || echo '{}' > "$CONF_DIR/credentials.json"
chown "$APP_UID:$APP_UID" "$CONF_DIR/edge.env" "$CONF_DIR/credentials.json"
chmod 600 "$CONF_DIR/edge.env" "$CONF_DIR/credentials.json"

if [[ -n ${TAILSCALE_AUTHKEY:-} ]]; then
    log "VPN: Tailscale"
    if ! command -v tailscale >/dev/null; then
        curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.noarmor.gpg \
            -o /usr/share/keyrings/tailscale-archive-keyring.gpg
        curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.tailscale-keyring.list \
            -o /etc/apt/sources.list.d/tailscale.list
        apt-get update -q && apt-get install -y -q tailscale
    fi
    tailscale up --authkey "$TAILSCALE_AUTHKEY" --hostname "$(hostname)"
fi

log "Edge service: $INSTALL_DIR, started at boot"
install -d "$INSTALL_DIR"
install -m 644 "$SCRIPT_DIR/docker-compose.yml" "$INSTALL_DIR/docker-compose.yml"
cat > /etc/systemd/system/cctv-edge.service <<EOF
[Unit]
Description=CCTV edge AI service
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$CONF_DIR/edge.env
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable cctv-edge.service
(cd "$INSTALL_DIR" && set -a && . "$CONF_DIR/edge.env" && set +a && docker compose pull) \
    || echo "warning: could not pull the edge image yet (registry login needed?)"
systemctl restart cctv-edge.service || echo "warning: edge service did not start; see 'docker compose logs'"

log "Done. Next: add camera credentials to $CONF_DIR/credentials.json, then: systemctl restart cctv-edge"
