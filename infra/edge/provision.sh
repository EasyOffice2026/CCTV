#!/usr/bin/env bash
# Provision a Jetson (JetPack 6 / Ubuntu 22.04) as a CCTV edge box. Safe to run again.
#
#   sudo ./provision.sh
#
# Optional environment variables:
#   CCTV_EDGE_CLOUD_URL    cloud API URL (prompted if unset)
#   CCTV_EDGE_PAIRING_CODE one-time code from the admin panel (Edge devices > Register);
#                          prompted if unset. The box swaps it for its device token.
#   CCTV_EDGE_SERIAL       optional: the serial registered in the admin panel, checked at pairing
#   CCTV_EDGE_DEVICE_TOKEN use an existing device token instead of pairing
#   CCTV_EDGE_IMAGE        edge image to run (default: see docker-compose.yml)
#   CCTV_EDGE_STUN_URL     STUN server go2rtc uses for live view (default: Google's public one)
#   CCTV_EDGE_UPDATE_SIGNER  software updates (plan 2.10): cosign certificate identity
#                          (regexp) updates must be signed with; default: our release workflow
#   CCTV_EDGE_UPDATE_UNSIGNED=yes  lab boxes only: accept unsigned updates
#   NVME_DEVICE            e.g. /dev/nvme0n1, mounted at /var/lib/cctv-edge for footage
#   FORMAT_NVME=yes        format NVME_DEVICE as ext4 if it has no filesystem yet
#   ENCRYPT_NVME=yes       with FORMAT_NVME: encrypt it first (LUKS2, plan 7.2); the key is
#                          kept on the internal eMMC (/etc/cctv-edge/luks.key), so a removed
#                          or discarded NVMe can't be read (see docs/security.md)
#   TAILSCALE_AUTHKEY      join the support VPN (tailnet) for remote maintenance
#   MODELS_SOURCE          folder of model folders (each with model.json) to install to
#                          /opt/cctv-edge/models; never from git (see edge/README.md "Models")
#   ALLOW_NON_JETSON=yes   allow running on a non-Jetson Ubuntu box (lab/testing)
#
# Camera passwords go in /etc/cctv-edge/credentials.json afterwards (see edge/README.md).
# Without models the box runs motion detection only; copy them in later and restart.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_UID=10001                     # the service user inside the edge image
CONF_DIR=/etc/cctv-edge
DATA_DIR=/var/lib/cctv-edge
INSTALL_DIR=/opt/cctv-edge
MODELS_DIR=$INSTALL_DIR/models   # mounted read-only into the container

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
apt-get install -y -q docker.io docker-compose-v2 chrony curl jq python3
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
install -d -m 700 "$CONF_DIR"
if [[ -n ${NVME_DEVICE:-} ]]; then
    log "Storage: $NVME_DEVICE -> $DATA_DIR"
    [[ -b $NVME_DEVICE ]] || die "$NVME_DEVICE is not a block device"
    data_device="$NVME_DEVICE"
    if ! blkid "$NVME_DEVICE" >/dev/null 2>&1; then
        [[ ${FORMAT_NVME:-} == yes ]] || die "$NVME_DEVICE has no filesystem; set FORMAT_NVME=yes to format it"
        if [[ ${ENCRYPT_NVME:-} == yes ]]; then
            apt-get install -y -q cryptsetup
            ( umask 077 && head -c 64 /dev/urandom > "$CONF_DIR/luks.key" )
            chmod 400 "$CONF_DIR/luks.key"
            cryptsetup luksFormat --type luks2 --batch-mode --key-file "$CONF_DIR/luks.key" "$NVME_DEVICE"
            cryptsetup open --key-file "$CONF_DIR/luks.key" "$NVME_DEVICE" cctv-data
            mkfs.ext4 -L cctv-data /dev/mapper/cctv-data
        else
            mkfs.ext4 -L cctv-data "$NVME_DEVICE"
        fi
    fi
    if [[ $(blkid -s TYPE -o value "$NVME_DEVICE") == crypto_LUKS ]]; then
        [[ -f $CONF_DIR/luks.key ]] || die "$NVME_DEVICE is encrypted but $CONF_DIR/luks.key is missing"
        luks_uuid="$(blkid -s UUID -o value "$NVME_DEVICE")"
        grep -q "^cctv-data " /etc/crypttab 2>/dev/null \
            || echo "cctv-data UUID=$luks_uuid $CONF_DIR/luks.key luks,discard,nofail" >> /etc/crypttab
        [[ -e /dev/mapper/cctv-data ]] \
            || cryptsetup open --key-file "$CONF_DIR/luks.key" "$NVME_DEVICE" cctv-data
        data_device=/dev/mapper/cctv-data
    fi
    uuid="$(blkid -s UUID -o value "$data_device")"
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
    [[ -n $cloud_url ]] || die "the cloud URL is required"
    if [[ -z $token ]]; then
        code="${CCTV_EDGE_PAIRING_CODE:-}"
        [[ -n $code ]] || read -rp "Pairing code (XXXX-XXXX): " code
        # With CCTV_EDGE_SERIAL set, the cloud also checks it matches the registered serial.
        body="$(jq -n --arg c "$code" --arg s "${CCTV_EDGE_SERIAL:-}" \
            '{pairing_code: $c} + (if $s == "" then {} else {serial: $s} end)')"
        response="$(curl -fsS -X POST "${cloud_url%/}/v1/edge/pair" \
            -H 'Content-Type: application/json' -d "$body")" \
            || die "pairing failed: check the code (it works once and expires)"
        token="$(jq -r .device_token <<<"$response")"
        echo "Paired as device $(jq -r .device_id <<<"$response")"
    fi
    umask 077
    cat > "$CONF_DIR/edge.env" <<EOF
CCTV_EDGE_CLOUD_URL=$cloud_url
CCTV_EDGE_DEVICE_TOKEN=$token
EOF
    [[ -z ${CCTV_EDGE_IMAGE:-} ]] || echo "CCTV_EDGE_IMAGE=$CCTV_EDGE_IMAGE" >> "$CONF_DIR/edge.env"
    [[ -z ${CCTV_EDGE_STUN_URL:-} ]] || echo "CCTV_EDGE_STUN_URL=$CCTV_EDGE_STUN_URL" >> "$CONF_DIR/edge.env"
    [[ -z ${CCTV_EDGE_UPDATE_SIGNER:-} ]] \
        || echo "CCTV_EDGE_UPDATE_SIGNER=$CCTV_EDGE_UPDATE_SIGNER" >> "$CONF_DIR/edge.env"
    [[ ${CCTV_EDGE_UPDATE_UNSIGNED:-} != yes ]] || echo "CCTV_EDGE_UPDATE_UNSIGNED=yes" >> "$CONF_DIR/edge.env"
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
# go2rtc (live view / playback over WebRTC). Its API stays on localhost; browsers reach
# port 8555 (TCP+UDP), so allow it in the branch firewall unless all viewers use TURN.
install -m 644 "$SCRIPT_DIR/go2rtc.yaml" "$INSTALL_DIR/go2rtc.yaml"
# AI models: weights are licensed separately and are never in git. Each model folder has a
# model.json naming its licence and the sha256 the service checks before loading it.
install -d -m 755 "$MODELS_DIR"
if [[ -n ${MODELS_SOURCE:-} ]]; then
    [[ -d $MODELS_SOURCE ]] || die "MODELS_SOURCE $MODELS_SOURCE is not a folder"
    for model in "$MODELS_SOURCE"/*/; do
        [[ -f $model/model.json ]] || { echo "skipping $model (no model.json)"; continue; }
        rm -rf "${MODELS_DIR:?}/$(basename "$model")"
        cp -r "$model" "$MODELS_DIR/"
        echo "model installed: $(basename "$model")"
    done
    chmod -R a+rX "$MODELS_DIR"
fi
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
# Software updates (plan 2.10): every minute the updater looks for a release the cloud
# offered (the service writes it to $DATA_DIR/update), checks its signature, switches and
# rolls back if the new version doesn't report in. Log: journalctl -u cctv-edge-update.
install -m 755 "$SCRIPT_DIR/../../edge/cctv_edge/updater.py" "$INSTALL_DIR/updater.py"
cat > /etc/systemd/system/cctv-edge-update.service <<EOF
[Unit]
Description=CCTV edge software update
Requires=docker.service
After=docker.service cctv-edge.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $INSTALL_DIR/updater.py
TimeoutStartSec=45min
EOF
cat > /etc/systemd/system/cctv-edge-update.timer <<EOF
[Unit]
Description=Look for a CCTV edge software update every minute

[Timer]
OnBootSec=2min
OnUnitInactiveSec=1min

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable cctv-edge.service
systemctl enable --now cctv-edge-update.timer
docker pull "$(grep -oP '^DEFAULT_COSIGN_IMAGE = "\K[^"]+' "$INSTALL_DIR/updater.py")" >/dev/null \
    || echo "warning: could not pull cosign yet; updates will retry it"
(cd "$INSTALL_DIR" && set -a && . "$CONF_DIR/edge.env" && set +a && docker compose pull) \
    || echo "warning: could not pull the edge image yet (registry login needed?)"
systemctl restart cctv-edge.service || echo "warning: edge service did not start; see 'docker compose logs'"

log "Done. Next: add camera credentials to $CONF_DIR/credentials.json, then: systemctl restart cctv-edge"
