#!/usr/bin/env bash
#
# deploy.sh — production installer / updater for the pgbench-harness web app.
#
# One command an operator runs on a fresh or existing Ubuntu 24.04 droplet to
# install, update, or remove the self-hosted web UI that wraps the
# `pgbench-harness` Python CLI.
#
# Usage:
#   sudo ./deploy.sh                       # install (or auto-update if installed)
#   sudo ./deploy.sh --update              # force update path
#   sudo ./deploy.sh --regen-certs         # reinstall + regenerate TLS cert
#   sudo ./deploy.sh --uninstall           # remove services + code, keep data
#   sudo ./deploy.sh --uninstall --purge   # remove everything incl. data dir
#   sudo ./deploy.sh --help
#
# See OPERATIONS.md for the full runbook.
#
set -euo pipefail

# ----------------------------------------------------------------------------
# Constants — the install contract. Do not change casually; the Python app and
# the systemd units are built to match these exact paths and env var names.
# ----------------------------------------------------------------------------
APP_DIR="/opt/pgbench-harness"
VENV_DIR="${APP_DIR}/venv"
DATA_DIR="/var/lib/pgbench-harness"
RESULTS_DIR="${DATA_DIR}/results"
DB_PATH="${DATA_DIR}/pgbench.db"
SECRET_KEY="${DATA_DIR}/secret.key"
CERT_DIR="${DATA_DIR}/certs"
CERT_PEM="${CERT_DIR}/cert.pem"
KEY_PEM="${CERT_DIR}/key.pem"
VERSION_MARKER="${DATA_DIR}/INSTALLED_VERSION"
LOG_DIR="/var/log/pgbench-harness"
INSTALL_LOG="${LOG_DIR}/install.log"
ENV_FILE="/etc/pgbench-harness.env"
SECRETS_ENV_FILE="/etc/pgbench-harness.secrets.env"
SYSBENCH_TPCC_DIR="/opt/sysbench-tpcc"
SYSBENCH_TPCC_REPO="https://github.com/Percona-Lab/sysbench-tpcc"

SVC_USER="pgbench"
SVC_GROUP="pgbench"

WEB_UNIT="pgbench-web.service"
WORKER_UNIT="pgbench-worker.service"
SYSTEMD_DIR="/etc/systemd/system"

# venv entrypoints (as installed by `pip install '.[web]'`)
BIN_WEB="${VENV_DIR}/bin/pgbench-web"
BIN_WORKER="${VENV_DIR}/bin/pgbench-worker"
BIN_PY="${VENV_DIR}/bin/python"
BIN_HARNESS="${VENV_DIR}/bin/pgbench-harness"

# Where this script lives (the checkout we may copy from).
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "${SCRIPT_PATH}")"

# ----------------------------------------------------------------------------
# Defaults / flag state
# ----------------------------------------------------------------------------
MODE="auto"            # auto | install | update | uninstall
PORT="8443"
BIND="0.0.0.0"
ADMIN_USER="admin"
PUBLIC_IP=""           # empty => auto-detect
REGEN_CERTS="no"
PURGE="no"
CERT_DAYS="825"

# ----------------------------------------------------------------------------
# Logging helpers. Everything goes to stdout AND the install log. The log is
# set up early (before arg parsing finishes) once we know we are root.
# ----------------------------------------------------------------------------
C_RESET=""; C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_BOLD=""
if [[ -t 1 ]]; then
  C_RESET="\033[0m"; C_RED="\033[31m"; C_GRN="\033[32m"
  C_YEL="\033[33m"; C_BLU="\033[34m"; C_BOLD="\033[1m"
fi

_log_to_file() {
  # Append to install log if the dir exists; ignore failures (pre-root, etc.).
  if [[ -d "${LOG_DIR}" ]]; then
    printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$1" >>"${INSTALL_LOG}" 2>/dev/null || true
  fi
}
section() { printf "\n${C_BOLD}${C_BLU}==> %s${C_RESET}\n" "$*"; _log_to_file "==> $*"; }
info()    { printf "    %s\n" "$*"; _log_to_file "    $*"; }
ok()      { printf "    ${C_GRN}OK${C_RESET} %s\n" "$*"; _log_to_file "OK $*"; }
warn()    { printf "    ${C_YEL}WARN${C_RESET} %s\n" "$*" >&2; _log_to_file "WARN $*"; }
err()     { printf "${C_RED}ERROR:${C_RESET} %s\n" "$*" >&2; _log_to_file "ERROR $*"; }

# ----------------------------------------------------------------------------
# Error trap: report the failing line + a friendly hint, then exit non-zero.
# ----------------------------------------------------------------------------
on_error() {
  local exit_code=$?
  local line_no=$1
  err "deploy.sh failed at line ${line_no} (exit ${exit_code})."
  err "The command on that line returned a non-zero status."
  if [[ -f "${INSTALL_LOG}" ]]; then
    err "Full log: ${INSTALL_LOG}"
  fi
  err "Common causes: missing network/apt access, the app package failed to"
  err "build in the venv, or a path is not writable. Re-run after fixing, or"
  err "see OPERATIONS.md -> Troubleshooting."
  exit "${exit_code}"
}
trap 'on_error ${LINENO}' ERR

usage() {
  cat <<'EOF'
deploy.sh — installer/updater for the pgbench-harness web app (Ubuntu 24.04)

USAGE:
  sudo ./deploy.sh [FLAGS]

FLAGS:
  --update             Force the update path (git pull + pip + migrate + restart).
                       Auto-detected when an install marker already exists.
  --port <n>           HTTPS port the web UI binds (default: 8443).
  --bind <addr>        Bind address (default: 0.0.0.0).
  --regen-certs        Regenerate the self-signed TLS cert (otherwise existing
                       certs are preserved).
  --admin-user <u>     Admin username to create/upsert (default: admin).
                       Password from $PGBENCH_ADMIN_PASSWORD or interactive prompt.
  --public-ip <ip>     Public IP for the cert SAN + printed URL (default: auto-detect).
  --uninstall          Stop + disable + remove services and /opt code. Keeps data.
  --purge              With --uninstall, ALSO delete /var/lib/pgbench-harness
                       (results, db, secret key, certs). Destructive.
  --help               Show this help.

ENV:
  PGBENCH_ADMIN_PASSWORD   If set, used non-interactively for create-admin.

EXAMPLES:
  sudo ./deploy.sh
  sudo PGBENCH_ADMIN_PASSWORD='s3cret' ./deploy.sh --admin-user ops
  sudo ./deploy.sh --update
  sudo ./deploy.sh --regen-certs --public-ip 203.0.113.10
  sudo ./deploy.sh --uninstall --purge

See OPERATIONS.md for the complete runbook.
EOF
}

# ----------------------------------------------------------------------------
# Arg parsing
# ----------------------------------------------------------------------------
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --update)      MODE="update" ;;
      --uninstall)   MODE="uninstall" ;;
      --purge)       PURGE="yes" ;;
      --regen-certs) REGEN_CERTS="yes" ;;
      --port)        PORT="${2:?--port needs a value}"; shift ;;
      --port=*)      PORT="${1#*=}" ;;
      --bind)        BIND="${2:?--bind needs a value}"; shift ;;
      --bind=*)      BIND="${1#*=}" ;;
      --admin-user)  ADMIN_USER="${2:?--admin-user needs a value}"; shift ;;
      --admin-user=*) ADMIN_USER="${1#*=}" ;;
      --public-ip)   PUBLIC_IP="${2:?--public-ip needs a value}"; shift ;;
      --public-ip=*) PUBLIC_IP="${1#*=}" ;;
      --help|-h)     usage; exit 0 ;;
      *) err "Unknown argument: $1"; echo; usage; exit 2 ;;
    esac
    shift
  done

  # Validate port is numeric and in range.
  if ! [[ "${PORT}" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
    err "Invalid --port '${PORT}' (must be 1-65535)."; exit 2
  fi
  if [[ "${PURGE}" == "yes" && "${MODE}" != "uninstall" ]]; then
    err "--purge is only valid together with --uninstall."; exit 2
  fi
}

# ----------------------------------------------------------------------------
# Preconditions
# ----------------------------------------------------------------------------
require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    err "This script must be run as root. Re-run with: sudo ${SCRIPT_PATH} $*"
    exit 1
  fi
}

require_ubuntu() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    if [[ "${ID:-}" != "ubuntu" ]]; then
      warn "Target OS is Ubuntu 24.04; detected ID='${ID:-unknown}'. Continuing, but this is untested."
    elif [[ "${VERSION_ID:-}" != "24.04" ]]; then
      warn "Tested on Ubuntu 24.04; detected ${VERSION_ID:-unknown}. Continuing."
    fi
  else
    warn "/etc/os-release not found; cannot verify OS. Continuing."
  fi
}

init_logging() {
  install -d -m 0755 "${LOG_DIR}"
  touch "${INSTALL_LOG}"
  chmod 0644 "${INSTALL_LOG}"
  _log_to_file "===== deploy.sh start: mode=${MODE} args=[$*] ====="
}

is_installed() {
  [[ -f "${VERSION_MARKER}" ]]
}

# ----------------------------------------------------------------------------
# Step: apt dependencies + sysbench pgsql driver verification
# ----------------------------------------------------------------------------
install_packages() {
  section "Installing OS packages"
  export DEBIAN_FRONTEND=noninteractive
  info "apt-get update ..."
  apt-get update -y
  info "Installing: postgresql-client sysbench git python3-venv python3-pip openssl curl"
  apt-get install -y --no-install-recommends \
    postgresql-client sysbench git python3-venv python3-pip openssl curl ca-certificates rsync
  ok "Packages installed."

  # kubectl for the Cluster Ops module (pinned to upstream stable; optional —
  # a download failure must not break a benchmark-only install).
  if command -v kubectl >/dev/null 2>&1; then
    ok "kubectl already present."
  else
    info "Installing kubectl (Cluster Ops module)"
    KVER="$(curl -fsSL https://dl.k8s.io/release/stable.txt 2>/dev/null || true)"
    if [[ -n "${KVER}" ]] && curl -fsSL -o /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KVER}/bin/linux/amd64/kubectl" 2>/dev/null; then
      chmod 0755 /usr/local/bin/kubectl
      ok "kubectl ${KVER} installed."
    else
      warn "kubectl download failed — Cluster Ops will be unavailable until it is installed."
    fi
  fi

  section "Verifying sysbench has the PostgreSQL (pgsql) driver"
  if sysbench oltp_read_only --db-driver=pgsql help >/dev/null 2>&1; then
    ok "sysbench pgsql driver present."
  else
    err "sysbench is installed but the pgsql DB driver is NOT available."
    err "The harness drives PostgreSQL via sysbench's pgsql driver, which is missing."
    err "Fix options:"
    err "  - Install a sysbench build with pgsql support, e.g. the Percona apt repo:"
    err "      curl -fsSL https://repo.percona.com/apt/percona-release_latest.\$(lsb_release -sc)_all.deb -o /tmp/percona-release.deb"
    err "      apt-get install -y /tmp/percona-release.deb && percona-release setup pdps-8.0"
    err "      apt-get update && apt-get install -y sysbench"
    err "  - Or build sysbench from source with --with-pgsql."
    err "Then re-run this script."
    exit 1
  fi
}

# ----------------------------------------------------------------------------
# Step: sysbench-tpcc checkout
# ----------------------------------------------------------------------------
setup_sysbench_tpcc() {
  section "Setting up sysbench-tpcc checkout"
  if [[ -d "${SYSBENCH_TPCC_DIR}/.git" ]]; then
    ok "sysbench-tpcc already present at ${SYSBENCH_TPCC_DIR} (left as-is)."
  elif [[ -e "${SYSBENCH_TPCC_DIR}" ]]; then
    warn "${SYSBENCH_TPCC_DIR} exists but is not a git checkout; leaving untouched."
  else
    info "Cloning ${SYSBENCH_TPCC_REPO} -> ${SYSBENCH_TPCC_DIR}"
    git clone --depth 1 "${SYSBENCH_TPCC_REPO}" "${SYSBENCH_TPCC_DIR}"
    ok "Cloned sysbench-tpcc."
  fi
  chown -R "${SVC_USER}:${SVC_GROUP}" "${SYSBENCH_TPCC_DIR}" 2>/dev/null || true
}

# ----------------------------------------------------------------------------
# Step: service user + directory layout
# ----------------------------------------------------------------------------
setup_user_and_dirs() {
  section "Creating service user and directory layout"
  if id -u "${SVC_USER}" >/dev/null 2>&1; then
    ok "User '${SVC_USER}' already exists."
  else
    info "Creating system user '${SVC_USER}' (no login shell, home=${DATA_DIR})"
    useradd --system --home-dir "${DATA_DIR}" --no-create-home \
            --shell /usr/sbin/nologin "${SVC_USER}"
    ok "User created."
  fi

  # Data dirs (owned by service user).
  install -d -m 0750 -o "${SVC_USER}" -g "${SVC_GROUP}" "${DATA_DIR}"
  install -d -m 0750 -o "${SVC_USER}" -g "${SVC_GROUP}" "${RESULTS_DIR}"
  install -d -m 0750 -o "${SVC_USER}" -g "${SVC_GROUP}" "${CERT_DIR}"
  # Cluster Ops: the ONLY sanctioned home for path-referenced kubeconfigs — the
  # sandboxed worker (ProtectHome/ProtectSystem) cannot see files anywhere else.
  install -d -m 0700 -o "${SVC_USER}" -g "${SVC_GROUP}" "${DATA_DIR}/kubeconfigs"
  # Log dir (owned by service user so the app can write web.log/worker.log).
  install -d -m 0755 -o "${SVC_USER}" -g "${SVC_GROUP}" "${LOG_DIR}"
  ok "Directories ready under ${DATA_DIR} and ${LOG_DIR}."
}

# ----------------------------------------------------------------------------
# Step: place app code at /opt/pgbench-harness
#   - If run from inside a checkout, rsync the current dir into APP_DIR.
#   - If APP_DIR is already a git checkout (update), git pull.
# ----------------------------------------------------------------------------
sync_app_code() {
  section "Placing application code at ${APP_DIR}"
  install -d -m 0755 "${APP_DIR}"

  if [[ -d "${APP_DIR}/.git" ]]; then
    info "${APP_DIR} is a git checkout; updating with git fetch/pull."
    git -C "${APP_DIR}" config --global --add safe.directory "${APP_DIR}" 2>/dev/null || true
    if git -C "${APP_DIR}" rev-parse --abbrev-ref HEAD >/dev/null 2>&1; then
      git -C "${APP_DIR}" fetch --all --prune
      # Fast-forward the current branch; fall back to reset if needed.
      if ! git -C "${APP_DIR}" pull --ff-only; then
        warn "Fast-forward pull failed; hard-resetting to origin of current branch."
        local br; br="$(git -C "${APP_DIR}" rev-parse --abbrev-ref HEAD)"
        git -C "${APP_DIR}" reset --hard "origin/${br}"
      fi
      ok "Updated existing git checkout."
    else
      warn "Detached HEAD in ${APP_DIR}; leaving code as-is."
    fi
  elif [[ "${SCRIPT_DIR}" != "${APP_DIR}" ]]; then
    # Running from a checkout that is not the install dir: copy it in.
    info "Syncing checkout ${SCRIPT_DIR} -> ${APP_DIR} (rsync)."
    rsync -a --delete \
      --exclude '.venv/' \
      --exclude 'venv/' \
      --exclude '__pycache__/' \
      --exclude '.mypy_cache/' \
      --exclude '.pytest_cache/' \
      --exclude 'build/' \
      --exclude 'dist/' \
      --exclude '*.egg-info/' \
      "${SCRIPT_DIR}/" "${APP_DIR}/"
    ok "Code synced into ${APP_DIR}."
  else
    ok "Script already runs from ${APP_DIR}; code in place."
  fi

  chown -R "${SVC_USER}:${SVC_GROUP}" "${APP_DIR}"
}

# ----------------------------------------------------------------------------
# Step: create/refresh the venv and install the package with the [web] extra.
# ----------------------------------------------------------------------------
setup_venv() {
  section "Creating Python virtual environment and installing the app"
  if [[ ! -x "${BIN_PY}" ]]; then
    info "Creating venv at ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
    ok "venv created."
  else
    ok "venv already exists at ${VENV_DIR} (reused)."
  fi

  info "Upgrading pip/setuptools/wheel in the venv"
  "${BIN_PY}" -m pip install --upgrade pip setuptools wheel >/dev/null

  info "Installing pgbench-harness with the 'web' extra: pip install '${APP_DIR}[web]'"
  "${BIN_PY}" -m pip install "${APP_DIR}[web]"
  ok "Application installed into the venv."

  # Sanity: entrypoints should now be on the venv PATH.
  for b in "${BIN_WEB}" "${BIN_WORKER}"; do
    if [[ ! -x "${b}" ]]; then
      err "Expected entrypoint not found after install: ${b}"
      err "Does the package define console-scripts for the web app and the 'web' extra?"
      exit 1
    fi
  done
  ok "Verified entrypoints: pgbench-web, pgbench-worker."

  chown -R "${SVC_USER}:${SVC_GROUP}" "${VENV_DIR}"
}

# ----------------------------------------------------------------------------
# Step: detect public IP (for cert SAN + printed URL)
# ----------------------------------------------------------------------------
detect_public_ip() {
  if [[ -n "${PUBLIC_IP}" ]]; then
    echo "${PUBLIC_IP}"; return 0
  fi
  local ip=""
  # 1) DigitalOcean metadata service.
  ip="$(curl -fs --max-time 3 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address 2>/dev/null || true)"
  # 2) Public reflectors.
  if [[ -z "${ip}" ]]; then
    ip="$(curl -fs --max-time 5 https://api.ipify.org 2>/dev/null || true)"
  fi
  if [[ -z "${ip}" ]]; then
    ip="$(curl -fs --max-time 5 https://ifconfig.me 2>/dev/null || true)"
  fi
  echo "${ip}"
}

# ----------------------------------------------------------------------------
# Step: generate self-signed TLS cert with SAN for IP + hostname.
# ----------------------------------------------------------------------------
generate_certs() {
  section "TLS certificate"
  if [[ -f "${CERT_PEM}" && -f "${KEY_PEM}" && "${REGEN_CERTS}" != "yes" ]]; then
    ok "Existing cert found and --regen-certs not set; keeping current cert."
    return 0
  fi
  if [[ "${REGEN_CERTS}" == "yes" && -f "${CERT_PEM}" ]]; then
    info "--regen-certs set: regenerating TLS certificate."
  else
    info "No existing certificate; generating a new self-signed cert."
  fi

  local ip host san_entries
  ip="$(detect_public_ip)"
  host="$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo '')"

  # Build SAN list.
  san_entries=""
  local i=1
  if [[ -n "${host}" ]]; then
    san_entries+="DNS.${i} = ${host}"$'\n'; ((i++))
  fi
  san_entries+="DNS.${i} = localhost"$'\n'; ((i++))
  local j=1
  if [[ -n "${ip}" ]]; then
    san_entries+="IP.${j} = ${ip}"$'\n'; ((j++))
  fi
  san_entries+="IP.${j} = 127.0.0.1"$'\n'

  local cn="${ip:-${host:-pgbench-harness}}"
  local cnf; cnf="$(mktemp)"
  cat >"${cnf}" <<EOF
[req]
distinguished_name = dn
x509_extensions = v3_req
prompt = no
[dn]
CN = ${cn}
O = pgbench-harness
[v3_req]
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names
[alt_names]
${san_entries}
EOF

  info "Generating ${CERT_DAYS}-day self-signed cert (CN=${cn})"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "${KEY_PEM}" -out "${CERT_PEM}" \
    -days "${CERT_DAYS}" -config "${cnf}" >/dev/null 2>&1
  rm -f "${cnf}"

  chown "${SVC_USER}:${SVC_GROUP}" "${CERT_PEM}" "${KEY_PEM}"
  chmod 0644 "${CERT_PEM}"
  chmod 0600 "${KEY_PEM}"
  ok "Wrote ${CERT_PEM} and ${KEY_PEM} (key mode 0600)."
}

# ----------------------------------------------------------------------------
# Step: secret key (used by the app to encrypt stored secrets). The installer
# only guarantees the file exists with 0600; the app generates/uses it.
# Never regenerated on update — losing it makes stored secrets undecryptable.
# ----------------------------------------------------------------------------
ensure_secret_key() {
  section "Application secret key"
  if [[ -f "${SECRET_KEY}" ]]; then
    chmod 0600 "${SECRET_KEY}" 2>/dev/null || true
    chown "${SVC_USER}:${SVC_GROUP}" "${SECRET_KEY}" 2>/dev/null || true
    ok "Secret key already present (left untouched)."
  else
    info "Generating ${SECRET_KEY} (0600) — used to encrypt stored secrets."
    # Must be a valid Fernet key: url-safe base64 of exactly 32 bytes (44 chars).
    ( umask 077; openssl rand -base64 32 | tr '+/' '-_' >"${SECRET_KEY}" )
    chown "${SVC_USER}:${SVC_GROUP}" "${SECRET_KEY}"
    chmod 0600 "${SECRET_KEY}"
    ok "Secret key created. BACK THIS UP (see OPERATIONS.md)."
  fi
}

# ----------------------------------------------------------------------------
# Step: operator-managed secrets env file (worker only). Created empty if
# missing, NEVER overwritten on update — this is where credentials like the
# PMM service-account token live, so they must not be in the 0644 env file
# that write_env_file regenerates on every deploy. The worker unit loads it
# with EnvironmentFile=- (the '-' means "optional"), so an empty file is fine.
# ----------------------------------------------------------------------------
ensure_secrets_env() {
  section "Secrets environment file (worker)"
  if [[ -f "${SECRETS_ENV_FILE}" ]]; then
    chmod 0600 "${SECRETS_ENV_FILE}" 2>/dev/null || true
    chown root:root "${SECRETS_ENV_FILE}" 2>/dev/null || true
    ok "Secrets env file already present (left untouched)."
  else
    info "Creating ${SECRETS_ENV_FILE} (0600, root-only)."
    ( umask 077; cat >"${SECRETS_ENV_FILE}" <<'SECRETS'
# pgbench-harness worker secrets — read by pgbench-worker.service only.
# NOT managed by deploy.sh: this file is created once and never overwritten.
# systemd (root) reads it before dropping privileges, so 0600 root:root is
# correct even though the service runs as the pgbench user.
#
# PMM 3.x enablement (ops pmm-enable / pmm-status): uncomment and set the
# service-account token (normally starts with glsa_). It is read from the
# environment only — never put it in a spec, and the harness never writes it
# to disk, logs, or reports.
#PGB_PMM_TOKEN=glsa_replace_me
#
# After editing: sudo systemctl restart pgbench-worker
SECRETS
    )
    chown root:root "${SECRETS_ENV_FILE}"
    chmod 0600 "${SECRETS_ENV_FILE}"
    ok "Secrets env file created — add PGB_PMM_TOKEN there to enable PMM ops."
  fi
}

# ----------------------------------------------------------------------------
# Step: write the environment file consumed by both systemd units + the app.
# ----------------------------------------------------------------------------
write_env_file() {
  section "Writing environment file ${ENV_FILE}"
  cat >"${ENV_FILE}" <<EOF
# Managed by deploy.sh — edits may be overwritten on the next run.
# Consumed by ${WEB_UNIT}, ${WORKER_UNIT}, and the pgbench-harness web app.
# Secrets (e.g. PGB_PMM_TOKEN) do NOT belong here: put them in
# ${SECRETS_ENV_FILE} (0600, never touched by deploy.sh).
PGBENCH_DATA_DIR=${DATA_DIR}
PGBENCH_DB=${DB_PATH}
PGBENCH_BIND=${BIND}
PGBENCH_PORT=${PORT}
PGBENCH_TLS_CERT=${CERT_PEM}
PGBENCH_TLS_KEY=${KEY_PEM}
# Absolute path to the CLI the web/worker shell out to. systemd gives services a
# minimal PATH that excludes the venv bin, so a bare "pgbench-harness" would not
# resolve (doctor/preflight/runs would fail with "No such file or directory").
PGBENCH_HARNESS_BIN=${BIN_HARNESS}
EOF
  chmod 0644 "${ENV_FILE}"
  ok "Env file written (port=${PORT}, bind=${BIND})."
}

# ----------------------------------------------------------------------------
# Step: run idempotent DB migrations.
# ----------------------------------------------------------------------------
run_migrate() {
  section "Running database migrations (idempotent)"
  # Run as the service user so any DB file created is owned by pgbench, with
  # the env contract explicitly set. Prefer the documented subcommand; fall
  # back to the module form if that subcommand is not available.
  if sudo -u "${SVC_USER}" \
       env PGBENCH_DATA_DIR="${DATA_DIR}" PGBENCH_DB="${DB_PATH}" \
       "${BIN_WEB}" migrate >/dev/null 2>&1; then
    ok "Migrations applied via 'pgbench-web migrate'."
  else
    info "'pgbench-web migrate' unavailable; using module form."
    sudo -u "${SVC_USER}" \
      env PGBENCH_DATA_DIR="${DATA_DIR}" PGBENCH_DB="${DB_PATH}" \
      "${BIN_PY}" -m pgbench_webapp.db migrate
    ok "Migrations applied via 'python -m pgbench_webapp.db migrate'."
  fi
  # Ensure the db file ends up owned by the service user.
  if [[ -f "${DB_PATH}" ]]; then
    chown "${SVC_USER}:${SVC_GROUP}" "${DB_PATH}" 2>/dev/null || true
  fi
}

# ----------------------------------------------------------------------------
# Step: create/upsert the admin user (install only).
# ----------------------------------------------------------------------------
create_admin() {
  section "Admin account"
  local pw="${PGBENCH_ADMIN_PASSWORD:-}"
  if [[ -z "${pw}" ]]; then
    if [[ -t 0 ]]; then
      info "Set an admin password for user '${ADMIN_USER}' (input hidden)."
      local pw2=""
      read -rsp "    Password: " pw; echo
      read -rsp "    Confirm:  " pw2; echo
      if [[ -z "${pw}" || "${pw}" != "${pw2}" ]]; then
        err "Passwords empty or did not match. Aborting admin creation."
        exit 1
      fi
    else
      err "No PGBENCH_ADMIN_PASSWORD set and not running interactively."
      err "Re-run with: sudo PGBENCH_ADMIN_PASSWORD='...' ${SCRIPT_PATH} --admin-user ${ADMIN_USER}"
      exit 1
    fi
  else
    info "Using PGBENCH_ADMIN_PASSWORD for user '${ADMIN_USER}' (idempotent upsert)."
  fi

  # Pass the password via env to the create-admin command; run as service user.
  sudo -u "${SVC_USER}" \
    env PGBENCH_DATA_DIR="${DATA_DIR}" PGBENCH_DB="${DB_PATH}" \
        PGBENCH_ADMIN_PASSWORD="${pw}" \
    "${BIN_PY}" -m pgbench_webapp.admin create-admin --user "${ADMIN_USER}"
  ok "Admin user '${ADMIN_USER}' created/updated."
  unset pw
}

# ----------------------------------------------------------------------------
# Step: install + (re)load systemd units, enable + start services.
# ----------------------------------------------------------------------------
install_systemd_units() {
  section "Installing systemd units"
  local src_dir="${APP_DIR}/packaging/systemd"

  for unit in "${WEB_UNIT}" "${WORKER_UNIT}"; do
    if [[ -f "${src_dir}/${unit}" ]]; then
      info "Installing ${unit} from packaging dir."
      install -m 0644 "${src_dir}/${unit}" "${SYSTEMD_DIR}/${unit}"
    else
      warn "${src_dir}/${unit} not found; writing a built-in copy."
      write_builtin_unit "${unit}"
    fi
  done

  info "systemctl daemon-reload"
  systemctl daemon-reload
  info "Enabling and (re)starting services"
  systemctl enable "${WEB_UNIT}" "${WORKER_UNIT}" >/dev/null 2>&1 || true
  systemctl restart "${WEB_UNIT}"
  systemctl restart "${WORKER_UNIT}"

  # Give them a moment, then report status.
  sleep 2
  local failed=0
  for unit in "${WEB_UNIT}" "${WORKER_UNIT}"; do
    if systemctl is-active --quiet "${unit}"; then
      ok "${unit} is active."
    else
      err "${unit} is NOT active. Recent logs:"
      journalctl -u "${unit}" -n 20 --no-pager >&2 || true
      failed=1
    fi
  done
  if (( failed )); then
    err "One or more services failed to start. See OPERATIONS.md -> Troubleshooting."
    exit 1
  fi
}

# Fallback unit writer (used only if packaging/systemd/* is missing).
write_builtin_unit() {
  local unit="$1"
  case "${unit}" in
    "${WEB_UNIT}")
      cat >"${SYSTEMD_DIR}/${unit}" <<EOF
[Unit]
Description=pgbench-harness web UI (uvicorn, TLS)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${BIN_WEB}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=${DATA_DIR} ${LOG_DIR}

[Install]
WantedBy=multi-user.target
EOF
      ;;
    "${WORKER_UNIT}")
      cat >"${SYSTEMD_DIR}/${unit}" <<EOF
[Unit]
Description=pgbench-harness queue worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
EnvironmentFile=-${SECRETS_ENV_FILE}
ExecStart=${BIN_WORKER}
Restart=on-failure
RestartSec=3
KillMode=process
TimeoutStopSec=120
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=false
ReadWritePaths=${DATA_DIR} ${LOG_DIR}

[Install]
WantedBy=multi-user.target
EOF
      ;;
  esac
  chmod 0644 "${SYSTEMD_DIR}/${unit}"
}

# ----------------------------------------------------------------------------
# Step: record installed version.
# ----------------------------------------------------------------------------
write_version_marker() {
  local ver=""
  ver="$(${BIN_HARNESS} --version 2>/dev/null | awk '{print $NF}' || true)"
  if [[ -z "${ver}" ]]; then
    ver="$(git -C "${APP_DIR}" describe --tags --always --dirty 2>/dev/null || echo 'unknown')"
  fi
  printf '%s\n' "${ver}" >"${VERSION_MARKER}"
  chown "${SVC_USER}:${SVC_GROUP}" "${VERSION_MARKER}" 2>/dev/null || true
  echo "${ver}"
}

# ----------------------------------------------------------------------------
# Git SHA actually installed under APP_DIR (so "did my code land?" is answerable).
installed_sha() {
  git -C "${APP_DIR}" rev-parse --short HEAD 2>/dev/null || echo "(not a git checkout)"
}

# Whether the built operator-console SPA bundle is present in the install.
spa_present() {
  [[ -f "${APP_DIR}/src/pgbench_webapp/static/spa/index.html" ]]
}

# Final summary printed after a fresh install / regen.
# ----------------------------------------------------------------------------
# NOTE: the banner lines below intentionally embed our own color-escape
# constants (never user input) directly in the printf format string.
# shellcheck disable=SC2059
print_install_summary() {
  local ver="$1"
  local ip; ip="$(detect_public_ip)"
  local display_ip="${ip:-<your-droplet-ip>}"
  local fp=""
  if [[ -f "${CERT_PEM}" ]]; then
    fp="$(openssl x509 -in "${CERT_PEM}" -noout -fingerprint -sha256 2>/dev/null | sed 's/^.*=//')"
  fi

  printf "\n${C_BOLD}${C_GRN}========================================================${C_RESET}\n"
  printf   "${C_BOLD}${C_GRN} pgbench-harness web app is installed and running${C_RESET}\n"
  printf   "${C_BOLD}${C_GRN}========================================================${C_RESET}\n"
  printf "\n"
  printf "  Version       : %s  (git %s)\n" "${ver}" "$(installed_sha)"
  printf "  Console (UI)  : ${C_BOLD}https://%s:%s/ui${C_RESET}\n" "${display_ip}" "${PORT}"
  printf "  Classic UI    : https://%s:%s/   (legacy; the console at /ui is the new UI)\n" "${display_ip}" "${PORT}"
  printf "  Admin user    : %s\n" "${ADMIN_USER}"
  printf "  Health check  : https://%s:%s/healthz\n" "${display_ip}" "${PORT}"
  printf "  Data dir      : %s\n" "${DATA_DIR}"
  printf "  Logs (journal): journalctl -u %s -f   |   journalctl -u %s -f\n" "${WEB_UNIT}" "${WORKER_UNIT}"
  if spa_present; then
    printf "  Console build : present\n"
  else
    printf "  ${C_YEL}Console build : MISSING — /ui will show a placeholder. The classic UI at /\n"
    printf "                  still works. Build it (npm --prefix frontend ci && npm --prefix\n"
    printf "                  frontend run build) or install a release that ships the assets.${C_RESET}\n"
  fi
  printf "\n"

  printf "${C_BOLD}TLS certificate (SELF-SIGNED)${C_RESET}\n"
  printf "  ${C_YEL}This certificate is self-signed. Browsers will warn until you${C_RESET}\n"
  printf "  ${C_YEL}trust or pin it. Verify the fingerprint below before trusting.${C_RESET}\n"
  printf "  SHA-256 fingerprint:\n    %s\n" "${fp:-<run: openssl x509 -in ${CERT_PEM} -noout -fingerprint -sha256>}"
  printf "  Re-print anytime:\n    openssl x509 -in %s -noout -fingerprint -sha256\n" "${CERT_PEM}"
  printf "  Trust / pin options:\n"
  printf "    - Browser: open the URL, compare the cert fingerprint to the value\n"
  printf "      above, then accept the exception.\n"
  printf "    - curl: curl --cacert %s https://%s:%s/healthz\n" "${CERT_PEM}" "${display_ip}" "${PORT}"
  printf "    - System trust (Ubuntu client):\n"
  printf "        sudo cp <copied-cert>.pem /usr/local/share/ca-certificates/pgbench.crt && sudo update-ca-certificates\n"
  printf "\n"

  printf "${C_BOLD}Open the firewall for inbound TCP %s (DigitalOcean cloud firewall)${C_RESET}\n" "${PORT}"
  printf "  doctl (create a firewall and attach your droplet):\n"
  printf "    doctl compute firewall create \\\\\n"
  printf "      --name pgbench-web \\\\\n"
  printf "      --inbound-rules \"protocol:tcp,ports:%s,address:0.0.0.0/0,address:::/0\" \\\\\n" "${PORT}"
  printf "      --outbound-rules \"protocol:tcp,ports:all,address:0.0.0.0/0,address:::/0 protocol:udp,ports:all,address:0.0.0.0/0,address:::/0\" \\\\\n"
  printf "      --droplet-ids <DROPLET_ID>\n"
  printf "  Or add a rule to an existing firewall:\n"
  printf "    doctl compute firewall add-rules <FIREWALL_ID> \\\\\n"
  printf "      --inbound-rules \"protocol:tcp,ports:%s,address:0.0.0.0/0\"\n" "${PORT}"
  printf "  Console steps: Networking -> Firewalls -> (your firewall) ->\n"
  printf "    Inbound Rules -> New rule -> Custom TCP, Port %s, Sources: All IPv4/IPv6\n" "${PORT}"
  printf "    (Tighten 'Sources' to your office/VPN CIDR for production.)\n"
  printf "\n"
  printf "  Full runbook: %s/OPERATIONS.md\n\n" "${APP_DIR}"
}

# ----------------------------------------------------------------------------
# Uninstall
# ----------------------------------------------------------------------------
# shellcheck disable=SC2059  # color constants in printf format are ours, not user input
do_uninstall() {
  section "Uninstalling pgbench-harness web app"
  for unit in "${WEB_UNIT}" "${WORKER_UNIT}"; do
    if systemctl list-unit-files | grep -q "^${unit}"; then
      info "Stopping and disabling ${unit}"
      systemctl stop "${unit}" 2>/dev/null || true
      systemctl disable "${unit}" 2>/dev/null || true
    fi
    rm -f "${SYSTEMD_DIR}/${unit}"
  done
  systemctl daemon-reload
  rm -f "${ENV_FILE}"
  ok "Services stopped, disabled, and removed."

  if [[ -d "${APP_DIR}" ]]; then
    info "Removing application code at ${APP_DIR}"
    rm -rf "${APP_DIR}"
    ok "Removed ${APP_DIR}."
  fi

  if [[ "${PURGE}" == "yes" ]]; then
    warn "--purge set: deleting data dir ${DATA_DIR} (results, db, secret key, certs)."
    rm -rf "${DATA_DIR}"
    rm -rf "${LOG_DIR}"
    ok "Data and logs purged."
    info "The '${SVC_USER}' user is left in place; remove with: userdel ${SVC_USER}"
  else
    ok "Data dir preserved at ${DATA_DIR} (use --purge to delete it)."
    info "sysbench-tpcc checkout left at ${SYSBENCH_TPCC_DIR}."
    info "Service user '${SVC_USER}' left in place."
  fi
  printf "\n${C_GRN}Uninstall complete.${C_RESET}\n"
}

# ----------------------------------------------------------------------------
# Install / update flows
# ----------------------------------------------------------------------------
do_install() {
  section "Fresh install of pgbench-harness web app"
  install_packages
  setup_sysbench_tpcc
  setup_user_and_dirs
  sync_app_code
  setup_venv
  generate_certs
  ensure_secret_key
  ensure_secrets_env
  write_env_file
  run_migrate
  create_admin
  install_systemd_units
  local ver; ver="$(write_version_marker)"
  ok "Recorded INSTALLED_VERSION=${ver}"
  print_install_summary "${ver}"
}

# shellcheck disable=SC2059  # color constants in printf format are ours, not user input
do_update() {
  section "Updating pgbench-harness web app"
  local prev=""; [[ -f "${VERSION_MARKER}" ]] && prev="$(cat "${VERSION_MARKER}")"
  info "Previously installed version: ${prev:-unknown}"
  info "Update will NOT touch: results/, pgbench.db, secret.key, certs, secrets env, admin creds."

  # Ensure prerequisites in case the droplet drifted, but never touch data.
  install_packages
  setup_sysbench_tpcc
  setup_user_and_dirs        # idempotent; fixes perms, never deletes data
  sync_app_code
  setup_venv
  # Certs: only regenerate if explicitly requested.
  generate_certs
  ensure_secret_key          # only creates if missing; never overwrites
  ensure_secrets_env         # only creates if missing; never overwrites
  write_env_file             # reflects any --port/--bind change
  run_migrate
  install_systemd_units      # daemon-reload + restart both services
  local ver; ver="$(write_version_marker)"
  ok "Updated ${prev:-unknown} -> ${ver}."

  if [[ "${REGEN_CERTS}" == "yes" ]]; then
    print_install_summary "${ver}"
  else
    local ip; ip="$(detect_public_ip)"
    printf "\n${C_GRN}Update complete.${C_RESET} Services restarted.\n"
    printf "  Version : %s  (git %s)\n" "${ver}" "$(installed_sha)"
    printf "  Console : ${C_BOLD}https://%s:%s/ui${C_RESET}   (classic UI at /)\n" "${ip:-<droplet-ip>}" "${PORT}"
    printf "  Health  : https://%s:%s/healthz\n" "${ip:-<droplet-ip>}" "${PORT}"
    printf "  Logs    : journalctl -u %s -f\n" "${WEB_UNIT}"
    if ! spa_present; then
      printf "  ${C_YEL}Note: operator-console SPA bundle is missing; /ui will show a placeholder.${C_RESET}\n"
    fi
    printf "\n"
  fi
}

# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
main() {
  parse_args "$@"
  require_root "$@"
  require_ubuntu
  init_logging "$@"

  case "${MODE}" in
    uninstall)
      do_uninstall
      ;;
    update)
      if ! is_installed; then
        warn "No install marker at ${VERSION_MARKER}; running a fresh install instead."
        do_install
      else
        do_update
      fi
      ;;
    install)
      do_install
      ;;
    auto)
      if is_installed; then
        info "Existing install detected (${VERSION_MARKER}); switching to update mode."
        do_update
      else
        do_install
      fi
      ;;
  esac
}

main "$@"
