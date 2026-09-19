#!/usr/bin/env bash
#
# Deploy an MCP server's HTTP transport to Google Cloud Run, in a GCP project of
# its own, with sign-ins kept in Firestore.
#
#     MCP client ──https──> Cloud Run (your server) ──> Firestore (sign-ins)
#                                  │
#                                  └──> Google (proves who signed in)
#
# The server is its own OAuth 2.1 authorization server and delegates only the
# login to Google, because MCP clients need dynamic client registration and
# resource-scoped tokens, which Google does not offer.
#
# Re-running is how you update: the project is found by its app=<APP> label and
# everything that already exists is reused. It keeps no state of its own besides
# the client secret in Secret Manager, so pass the same settings every time.
#
# ---------------------------------------------------------------------------
# ADAPTING THIS SCRIPT
#
# Set APP (and optionally PREFIX) below, or export them. Nothing else in the
# script knows the server's name.
#
#   APP     lowercase; names the project, service, Firestore database, image
#           repository, secret, and service accounts.
#   PREFIX  the env-var prefix of the server's own configuration; defaults to
#           APP uppercased with hyphens as underscores.
#
# The server must read these at runtime (see references/server.md):
#
#   ${PREFIX}_PUBLIC_URL, ${PREFIX}_GOOGLE_CLIENT_ID, ${PREFIX}_GOOGLE_CLIENT_SECRET,
#   ${PREFIX}_ALLOWED_EMAILS, ${PREFIX}_ALLOWED_DOMAINS,
#   ${PREFIX}_ALLOW_ANY_GOOGLE_ACCOUNT, ${PREFIX}_TOKEN_STORE,
#   ${PREFIX}_FIRESTORE_DATABASE, ${PREFIX}_STATELESS_HTTP
#
# It also needs a Dockerfile at the repo root (see assets/Dockerfile) and, for
# the spend cap, the function source at $SPEND_CAP_SOURCE.
# ---------------------------------------------------------------------------
#
# Usage:
#   scripts/deploy-gcp.sh
#   ${PREFIX}_GOOGLE_CLIENT_ID=... ${PREFIX}_GOOGLE_CLIENT_SECRET=... \
#     ${PREFIX}_ALLOWED_EMAILS=you@gmail.com scripts/deploy-gcp.sh --non-interactive
#
# Settings (all optional; the script prompts for what it needs):
#   ${PREFIX}_GCP_PROJECT          default: the project labeled app=<APP>, else a
#                                  new <APP>-xxxxxx
#   ${PREFIX}_GCP_REGION           default us-east1
#   ${PREFIX}_GCP_SERVICE          Cloud Run service name, default <APP>
#   ${PREFIX}_GCP_BILLING_ACCOUNT  default: the only open billing account
#   ${PREFIX}_GCP_MAX_INSTANCES    default 1
#   ${PREFIX}_BUDGET_USD           monthly budget, default 5
#   ${PREFIX}_SHARED_BUDGET        true: another deployment in this project owns the
#                                  budget and kill switch, so create neither here
#                                  (see the mcp-servers-shared-cap repo)
#   ${PREFIX}_SPEND_CAP            true: unlink billing once spend nears the budget
#   ${PREFIX}_SPEND_CAP_AT         fraction of the budget that trips it, default 0.8
#   ${PREFIX}_SPEND_CAP_DRY_RUN    true: the kill switch only logs, for testing
#   ${PREFIX}_GOOGLE_CLIENT_ID, ${PREFIX}_GOOGLE_CLIENT_SECRET
#   ${PREFIX}_ALLOWED_EMAILS, ${PREFIX}_ALLOWED_DOMAINS, ${PREFIX}_ALLOW_ANY_GOOGLE_ACCOUNT
#   ${PREFIX}_DOMAIN               serve at this domain rather than the run.app URL,
#                                  which must be verified in Google Search Console
#
# Flags:
#   --non-interactive   never prompt; fail on anything missing
#   --reset-secret      ask for the client secret again, replacing the stored one
#   --teardown          delete the project and its budget, after confirming
#
# See references/cloud-run.md and references/spend-cap.md in the gcp-mcp-server skill.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
readonly SCRIPT_DIR REPO_ROOT

# ---- the only two names this script knows -----------------------------------
# APP names every GCP resource; PREFIX is the env-var prefix of the server's own
# configuration. Everything below is derived from them.
APP="${APP:-queensestate}"
PREFIX="${PREFIX:-$(printf '%s' "$APP" | tr 'a-z-' 'A-Z_')}"
[[ $APP =~ ^[a-z][-a-z0-9]{0,19}[a-z0-9]$ ]] \
  || { echo "APP must be 2-21 lowercase letters, digits, or hyphens: $APP" >&2; exit 2; }
readonly APP PREFIX

# Read ${PREFIX}_$1 from the environment, or print the default in $2.
cfg() { local name="${PREFIX}_$1"; printf '%s' "${!name:-${2:-}}"; }
# -----------------------------------------------------------------------------

readonly LABEL_KEY=app
readonly LABEL_VALUE="$APP"
# The MCP paths the server serves. Change only if the server differs.
readonly MCP_PATH="/mcp"
readonly CALLBACK_PATH="/auth/google/callback"
# Must match the collection names in the server's Firestore token store.
readonly TOKEN_COLLECTIONS="oauth_clients oauth_pending oauth_codes oauth_access_tokens oauth_refresh_tokens"
# Google's service account that publishes budget notifications to Pub/Sub.
readonly BUDGET_PUBLISHER=billing-budget-alert@system.gserviceaccount.com
readonly DOMAIN_RE='^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$'

REGION="$(cfg GCP_REGION us-east1)"
SERVICE="$(cfg GCP_SERVICE "$APP")"
MAX_INSTANCES="$(cfg GCP_MAX_INSTANCES 1)"
BUDGET_USD="$(cfg BUDGET_USD 5)"
SPEND_CAP_AT="$(cfg SPEND_CAP_AT 0.8)"

# Named after the service rather than fixed, so one project can hold several
# servers without them sharing a secret, an image repository, or - which would
# mix up their sign-ins - the same Firestore collections.
SECRET_NAME="$SERVICE-google-client-secret"
REPOSITORY="$SERVICE"
FIRESTORE_DATABASE="$SERVICE"
readonly SECRET_NAME REPOSITORY FIRESTORE_DATABASE

# The spend cap function's source. This repo no longer carries a copy: the cap
# is project-wide and lives in the mcp-servers-shared-cap repo, checked out
# beside this one. Only the per-server cap below reads it, which
# QUEENSESTATE_SHARED_BUDGET=true skips.
SPEND_CAP_SOURCE="${SPEND_CAP_SOURCE:-$REPO_ROOT/../mcp-servers-shared-cap/function}"
readonly SPEND_CAP_SOURCE

INTERACTIVE=1
TEARDOWN=0
RESET_SECRET=0

for arg in "$@"; do
  case "$arg" in
    --non-interactive) INTERACTIVE=0 ;;
    --reset-secret)    RESET_SECRET=1 ;;
    --teardown)        TEARDOWN=1 ;;
    -h|--help)
      awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
      exit 0 ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [[ -t 1 ]]; then
  B=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
else
  B=''; DIM=''; RED=''; GRN=''; YLW=''; RST=''
fi
readonly B DIM RED GRN YLW RST

step() { printf '\n%s==>%s %s%s%s\n' "$GRN" "$RST" "$B" "$*" "$RST"; }
info() { printf '    %s\n' "$*"; }
note() { printf '    %s%s%s\n' "$DIM" "$*" "$RST"; }
warn() { printf '%s !! %s%s\n' "$YLW" "$*" "$RST" >&2; }
die()  { printf '%s !! %s%s\n' "$RED" "$*" "$RST" >&2; exit 1; }

trap 'die "failed at line $LINENO"' ERR

WORK="$(mktemp -d)"
readonly WORK
trap 'rm -rf "$WORK"' EXIT

# gcloud must never stop to ask a question mid-run; the script asks up front.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

ask() { # ask VAR "prompt" [secret]
  local var=$1 prompt=$2 secret=${3:-} value="${!1:-}"
  [[ -n $value ]] && return 0
  (( INTERACTIVE )) || die "$var is not set and --non-interactive was given"
  if [[ -n $secret ]]; then read -rsp "    $prompt: " value; echo
  else read -rp "    $prompt: " value; fi
  [[ -n $value ]] || die "$var is required"
  printf -v "$var" '%s' "$value"
}

confirm() {
  (( INTERACTIVE )) || return 1
  local reply; read -rp "    $1 [y/N] " reply; [[ $reply == [yY]* ]]
}

is_true() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

lines() { # lines TEXT -> how many non-empty lines it has
  printf '%s\n' "$1" | awk 'NF { n++ } END { print n + 0 }'
}

random_suffix() {
  local hex
  hex="$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
  printf '%s' "${hex:0:6}"
}

# A comma- or space-separated list, lowercased and de-duplicated, as a bare
# comma-separated string: the form the server's allow-list settings take.
normalize_list() {
  local item out="" seen=","
  local -a items=()
  IFS=' ' read -r -a items <<< "$(printf '%s' "${1:-}" | tr '[:upper:],' '[:lower:] ')"
  for item in ${items[@]+"${items[@]}"}; do
    case "$seen" in *",$item,"*) continue ;; esac
    seen="$seen$item,"
    out="${out:+$out,}$item"
  done
  printf '%s' "$out"
}

gp() { gcloud --quiet --project="$PROJECT" "$@"; }

retry() { # retry CMD...: IAM is eventually consistent, so a new account can lag
  local attempt
  for attempt in 1 2 3 4 5; do
    "$@" && return 0
    note "not ready yet; retrying in $(( attempt * 5 ))s"
    sleep $(( attempt * 5 ))
  done
  "$@"
}

ensure_service_account() { # ensure_service_account NAME "Display name" -> email
  local email="$1@$PROJECT.iam.gserviceaccount.com" attempt
  if ! gp iam service-accounts describe "$email" >/dev/null 2>&1; then
    gp iam service-accounts create "$1" --display-name="$2" >/dev/null
    # A new account is not visible everywhere at once. Until it is, granting it
    # a role fails, and so does submitting a build that runs as it.
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
      gp iam service-accounts describe "$email" >/dev/null 2>&1 && break
      sleep 3
    done
  fi
  printf '%s' "$email"
}

grant_on_project() { # grant_on_project MEMBER ROLE
  retry gp projects add-iam-policy-binding "$PROJECT" \
    --member="$1" --role="$2" --condition=None >/dev/null
}

# Domain mappings for fully managed Cloud Run are beta-only in gcloud, so they
# are managed through the Cloud Run Admin API directly.
domains_api() { # domains_api METHOD [/NAME] [BODY_FILE]: body to $WORK/api.json, prints the HTTP status
  local -a data=()
  [[ -n ${3:-} ]] && data=(--data-binary "@$3")
  # The token goes in on stdin rather than the command line, where ps shows it.
  gcloud auth print-access-token | sed 's/^/Authorization: Bearer /' \
    | curl -sS -o "$WORK/api.json" -w '%{http_code}' -X "$1" -H @- \
        -H 'content-type: application/json' ${data[@]+"${data[@]}"} \
        "https://$REGION-run.googleapis.com/apis/domains.cloudrun.com/v1/namespaces/$PROJECT/domainmappings${2:-}" \
    || true
}

api_field() { # api_field route|records|error: read the last domains_api response
  # With gcloud's own interpreter, which every machine running gcloud has.
  "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    d = {}
field = sys.argv[1]
if field == "route":
    print((d.get("spec") or {}).get("routeName", ""))
elif field == "records":
    for r in (d.get("status") or {}).get("resourceRecords") or []:
        print("%-6s %-20s %s" % (r.get("type", ""), r.get("name") or "@", r.get("rrdata", "")))
elif field == "error":
    print((d.get("error") or {}).get("message", "no details"))
' "$1" < "$WORK/api.json"
}

ensure_domain_mapping() { # map $DOMAIN to the service and set DOMAIN_RECORDS
  local status route
  status="$(domains_api GET "/$DOMAIN")"
  if [[ $status == 200 ]]; then
    route="$(api_field route)"
    [[ $route == "$SERVICE" ]] || die "$DOMAIN is already mapped to $route, not $SERVICE"
    info "$DOMAIN is mapped to $SERVICE"
  elif [[ $status == 404 ]]; then
    cat > "$WORK/mapping.json" <<JSON
{
  "apiVersion": "domains.cloudrun.com/v1",
  "kind": "DomainMapping",
  "metadata": {"name": "$DOMAIN", "namespace": "$PROJECT"},
  "spec": {"routeName": "$SERVICE", "certificateMode": "AUTOMATIC"}
}
JSON
    status="$(domains_api POST "" "$WORK/mapping.json")"
    [[ $status == 20[01] ]] || die "could not map $DOMAIN (HTTP $status): $(api_field error)
    The domain must be verified in Google Search Console by $ACCOUNT:
      https://search.google.com/search-console"
    info "mapped $DOMAIN to $SERVICE"
  else
    die "could not look up the mapping for $DOMAIN (HTTP $status): $(api_field error)"
  fi
  # The DNS records are filled in a few seconds after the mapping is created.
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    DOMAIN_RECORDS="$(api_field records)"
    [[ -n $DOMAIN_RECORDS ]] && break
    sleep 5
    domains_api GET "/$DOMAIN" >/dev/null
  done
}

# -- preflight ---------------------------------------------------------------

step "Checking this machine"

command -v gcloud >/dev/null || die "gcloud is not installed: https://cloud.google.com/sdk/docs/install"
command -v git >/dev/null || die "git is required"
command -v curl >/dev/null || die "curl is required"
git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1 \
  || die "run this from a clone of the repository: the image is built from it"

ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
[[ -n $ACCOUNT ]] || die "gcloud is not logged in. Run: gcloud auth login"
gcloud auth print-access-token >/dev/null 2>&1 \
  || die "gcloud's login for $ACCOUNT has expired. Run: gcloud auth login"
info "gcloud account $ACCOUNT"

# Settings are checked before anything is created, so a typo costs a re-run only.
[[ $REGION =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || die "${PREFIX}_GCP_REGION is not a region: $REGION"
[[ $SERVICE =~ ^[a-z][-a-z0-9]{0,19}[a-z0-9]$ ]] \
  || die "${PREFIX}_GCP_SERVICE must be 2-21 lowercase letters, digits, or hyphens: $SERVICE"
[[ $MAX_INSTANCES =~ ^[1-9][0-9]*$ ]] || die "${PREFIX}_GCP_MAX_INSTANCES must be a positive integer"
[[ $BUDGET_USD =~ ^[0-9]+(\.[0-9]{1,2})?$ ]] || die "${PREFIX}_BUDGET_USD must be an amount like 5 or 5.50"
[[ $SPEND_CAP_AT =~ ^(0?\.[0-9]+|1(\.0+)?)$ ]] || die "${PREFIX}_SPEND_CAP_AT must be a fraction like 0.8"

DOMAIN="$(printf '%s' "$(cfg DOMAIN)" | tr '[:upper:]' '[:lower:]')"
DOMAIN="${DOMAIN%.}"
DOMAIN_RECORDS=""
if [[ -n $DOMAIN ]]; then
  [[ $DOMAIN =~ $DOMAIN_RE && ${#DOMAIN} -le 64 ]] \
    || die "${PREFIX}_DOMAIN must be a bare domain of at most 64 characters, like mcp.example.com: $DOMAIN"
  # Where Google offered domain mappings when this was written; a new region is
  # tried anyway, since the API refuses the mapping itself if it is unsupported.
  case "$REGION" in
    asia-east1|asia-northeast1|asia-southeast1|europe-north1|europe-west1|europe-west4) ;;
    us-central1|us-east1|us-east4|us-west1) ;;
    *) warn "Cloud Run may not offer domain mappings in $REGION; see references/cloud-run.md" ;;
  esac
  PY="$(gcloud info --format='value(basic.python_location)' 2>/dev/null || true)"
  [[ -n $PY && -x $PY ]] || die "could not find the Python interpreter gcloud runs on"
fi

# -- project -----------------------------------------------------------------

step "Project"

PROJECT="$(cfg GCP_PROJECT)"
if [[ -z $PROJECT ]]; then
  FOUND="$(gcloud projects list \
    --filter="labels.$LABEL_KEY=$LABEL_VALUE AND lifecycleState=ACTIVE" \
    --format='value(projectId)')"
  case "$(lines "$FOUND")" in
    0) PROJECT="" ;;
    1) PROJECT="$FOUND" ;;
    *) die "several projects are labeled $LABEL_KEY=$LABEL_VALUE; set ${PREFIX}_GCP_PROJECT to one of:
$FOUND" ;;
  esac
fi

PROJECT_STATE=""
if [[ -n $PROJECT ]]; then
  PROJECT_STATE="$(gcloud projects describe "$PROJECT" --format='value(lifecycleState)' 2>/dev/null || true)"
  [[ $PROJECT_STATE != DELETE_REQUESTED ]] \
    || die "project $PROJECT is pending deletion; restore it with: gcloud projects undelete $PROJECT"
fi

# -- teardown ----------------------------------------------------------------

if (( TEARDOWN )); then
  [[ $PROJECT_STATE == ACTIVE ]] || die "no $APP project to tear down"
  [[ "$(gcloud projects describe "$PROJECT" --format="value(labels.$LABEL_KEY)")" == "$LABEL_VALUE" ]] \
    || die "$PROJECT is not labeled $LABEL_KEY=$LABEL_VALUE; refusing to delete a project this script did not set up"
  (( INTERACTIVE )) || die "--teardown asks for confirmation, so it cannot run with --non-interactive"

  step "Tearing down $PROJECT"
  warn "this deletes project $PROJECT and everything in it"
  read -rp "    Type the project id to confirm: " REPLY_ID
  [[ $REPLY_ID == "$PROJECT" ]] || die "not confirmed; nothing was deleted"

  # The budget lives on the billing account, so deleting the project leaves it.
  BILLING="$(gcloud billing projects describe "$PROJECT" \
    --format='value(billingAccountName.basename())' 2>/dev/null || true)"
  if [[ -n $BILLING ]]; then
    for budget in $(gcloud billing budgets list --billing-account="$BILLING" \
        --billing-project="$PROJECT" --filter="displayName=\"$APP: $PROJECT\"" \
        --format='value(name.basename())' 2>/dev/null || true); do
      gcloud billing budgets delete "$budget" --billing-account="$BILLING" \
        --billing-project="$PROJECT" --quiet >/dev/null
      info "deleted budget $budget"
    done
  else
    note "no billing account is linked, so any budget must be removed in the console"
  fi
  gcloud projects delete "$PROJECT" --quiet
  info "project $PROJECT is scheduled for deletion; it can be restored for 30 days"
  exit 0
fi

if [[ -z $PROJECT ]]; then
  PROJECT="$APP-$(random_suffix)"
  info "no project is labeled $LABEL_KEY=$LABEL_VALUE; will create $PROJECT"
elif [[ -z $PROJECT_STATE ]]; then
  info "will create $PROJECT"
else
  info "using $PROJECT"
fi

# -- who may sign in ---------------------------------------------------------

step "Who may sign in"

ALLOW_ANY=false
is_true "$(cfg ALLOW_ANY_GOOGLE_ACCOUNT)" && ALLOW_ANY=true
ALLOWED_EMAILS="$(cfg ALLOWED_EMAILS)"
ALLOWED_DOMAINS="$(cfg ALLOWED_DOMAINS)"
if [[ $ALLOW_ANY == false && -z $ALLOWED_EMAILS && -z $ALLOWED_DOMAINS ]]; then
  note "or set ${PREFIX}_ALLOW_ANY_GOOGLE_ACCOUNT=true to admit every Google account"
  ask ALLOWED_EMAILS "Google address(es) allowed, comma-separated"
fi

EMAILS="$(normalize_list "$ALLOWED_EMAILS")"
DOMAINS="$(normalize_list "$ALLOWED_DOMAINS")"
for entry in ${EMAILS//,/ }; do
  [[ $entry != *[\"\\]* && $entry == ?*@?*.?* ]] || die "not an email address: $entry"
done
for entry in ${DOMAINS//,/ }; do
  [[ $entry =~ $DOMAIN_RE ]] || die "${PREFIX}_ALLOWED_DOMAINS entries must be bare domains: $entry"
done

if [[ $ALLOW_ANY == true ]]; then
  info "any Google account (the server is public)"
else
  [[ -n $EMAILS ]] && info "addresses: ${EMAILS//,/, }"
  [[ -n $DOMAINS ]] && info "domains:   ${DOMAINS//,/, }"
fi

# -- billing account ---------------------------------------------------------

step "Billing account"

BILLING="$(cfg GCP_BILLING_ACCOUNT)"
LINKED=""
BILLING_ENABLED=""
if [[ -n $PROJECT_STATE ]]; then
  LINKED="$(gcloud billing projects describe "$PROJECT" \
    --format='value(billingAccountName.basename())' 2>/dev/null || true)"
  BILLING_ENABLED="$(gcloud billing projects describe "$PROJECT" \
    --format='value(billingEnabled)' 2>/dev/null || true)"
fi
if [[ -z $BILLING && -n $LINKED && $BILLING_ENABLED == True ]]; then
  BILLING="$LINKED"
fi
if [[ -z $BILLING ]]; then
  ACCOUNTS="$(gcloud billing accounts list --filter=open=true \
    --format='value(name.basename(),displayName)')"
  case "$(lines "$ACCOUNTS")" in
    0) die "no open billing account; create one at https://console.cloud.google.com/billing" ;;
    1) BILLING="$(printf '%s' "$ACCOUNTS" | cut -f1)" ;;
    *)
      info "open billing accounts:"
      printf '%s\n' "$ACCOUNTS" | sed 's/^/      /'
      ask BILLING "Billing account id to use"
      ;;
  esac
fi
info "billing account $BILLING"

SHARED_BUDGET=false
is_true "$(cfg SHARED_BUDGET)" && SHARED_BUDGET=true
SPEND_CAP=false
is_true "$(cfg SPEND_CAP)" && SPEND_CAP=true
SPEND_CAP_DRY_RUN=false
is_true "$(cfg SPEND_CAP_DRY_RUN)" && SPEND_CAP_DRY_RUN=true
if [[ $SHARED_BUDGET == true ]]; then
  info "budget and spend cap are shared with the other servers in this project"
elif [[ $SPEND_CAP == true ]]; then
  info "budget \$$BUDGET_USD a month; billing is unlinked at ${SPEND_CAP_AT} of it"
else
  info "budget \$$BUDGET_USD a month (alerts only; ${PREFIX}_SPEND_CAP=true makes it a cap)"
fi

if (( INTERACTIVE )); then
  echo
  confirm "Create or update project $PROJECT in $REGION? It can incur charges." \
    || die "stopped before changing anything"
fi

# -- create ------------------------------------------------------------------

step "Setting up $PROJECT"

if [[ -z $PROJECT_STATE ]]; then
  gcloud projects create "$PROJECT" --name="$APP" \
    --labels="$LABEL_KEY=$LABEL_VALUE" --quiet >/dev/null
  info "created project $PROJECT"
elif [[ "$(gcloud projects describe "$PROJECT" --format="value(labels.$LABEL_KEY)")" != "$LABEL_VALUE" ]]; then
  # A project the script did not create keeps its own labels: it may hold other
  # servers. The label is only how a later run finds a project this made, and
  # --teardown refuses to delete anything that is not labelled, which is what
  # protects a shared project.
  note "$PROJECT is not labeled $LABEL_KEY=$LABEL_VALUE; pass ${PREFIX}_GCP_PROJECT=$PROJECT on later runs"
fi

if [[ $LINKED == "$BILLING" && $BILLING_ENABLED == True ]]; then
  info "billing is linked"
else
  gcloud billing projects link "$PROJECT" --billing-account="$BILLING" --quiet >/dev/null
  info "linked billing account $BILLING"
fi

APIS=(
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
  firestore.googleapis.com secretmanager.googleapis.com iam.googleapis.com
  cloudresourcemanager.googleapis.com billingbudgets.googleapis.com
)
[[ $SPEND_CAP == true ]] && APIS+=(
  pubsub.googleapis.com cloudfunctions.googleapis.com eventarc.googleapis.com
  cloudbilling.googleapis.com
)
info "enabling ${#APIS[@]} APIs (a minute on a new project)"
gp services enable "${APIS[@]}"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
# Cloud Run's deterministic URL, known before the first deploy.
RUN_URL="https://$SERVICE-$PROJECT_NUMBER.$REGION.run.app"
# The public URL is the OAuth issuer, so it has to be right the first time.
PUBLIC_URL="$RUN_URL"
[[ -n $DOMAIN ]] && PUBLIC_URL="https://$DOMAIN"
REDIRECT_URI="$PUBLIC_URL$CALLBACK_PATH"
# `gcloud run services logs read` is beta-only in older SDKs; this is GA.
LOGS_CMD="gcloud logging read 'resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$SERVICE\"' --project $PROJECT --limit 50"
info "service URL $RUN_URL"
[[ -n $DOMAIN ]] && info "public URL  $PUBLIC_URL"

# -- OAuth client ------------------------------------------------------------

step "Google OAuth client"

# The secret is asked for once; later runs reuse the version in Secret Manager
# unless ${PREFIX}_GOOGLE_CLIENT_SECRET is set or --reset-secret is given.
CLIENT_ID="$(cfg GOOGLE_CLIENT_ID)"
CLIENT_SECRET="$(cfg GOOGLE_CLIENT_SECRET)"
STORED_SECRET=""
if [[ -z $CLIENT_SECRET ]] && (( ! RESET_SECRET )); then
  STORED_SECRET="$(gp secrets versions access latest --secret="$SECRET_NAME" 2>/dev/null || true)"
fi
if [[ -z $CLIENT_ID || ( -z $CLIENT_SECRET && -z $STORED_SECRET ) ]]; then
  AUTHORIZED_DOMAIN_NOTE=""
  [[ -n $DOMAIN ]] && AUTHORIZED_DOMAIN_NOTE="
         Under Authorized domains, add the domain $DOMAIN belongs to."
  cat <<EOF

    ${B}Google has no API for creating OAuth clients${RST}, so this is the one
    manual step. In project $PROJECT:

      1. Branding:  https://console.cloud.google.com/auth/branding?project=$PROJECT
         An app name and your support email.$AUTHORIZED_DOMAIN_NOTE
      2. Audience:  https://console.cloud.google.com/auth/audience?project=$PROJECT
         External. Stay in Testing and add each allowed address as a test
         user - or Publish the app to let any Google account sign in.
      3. Clients:   https://console.cloud.google.com/auth/clients?project=$PROJECT
         Create client -> Web application -> Authorized redirect URIs, exactly:

           ${B}$REDIRECT_URI${RST}

    A client from another project works too: add this redirect URI to it.

EOF
fi
ask CLIENT_ID "Google client ID"
if [[ -z $CLIENT_SECRET && -n $STORED_SECRET ]]; then
  CLIENT_SECRET="$STORED_SECRET"
  info "reusing the client secret stored in $SECRET_NAME (--reset-secret to replace it)"
fi
ask CLIENT_SECRET "Google client secret" secret
[[ $CLIENT_ID =~ ^[A-Za-z0-9._-]+$ ]] || die "that does not look like an OAuth client ID"
[[ $CLIENT_ID == *.apps.googleusercontent.com ]] \
  || warn "client IDs normally end in .apps.googleusercontent.com"
info "client $CLIENT_ID"

# -- Firestore ---------------------------------------------------------------

step "Firestore"

if gp firestore databases describe --database="$FIRESTORE_DATABASE" >/dev/null 2>&1; then
  info "database $FIRESTORE_DATABASE exists"
else
  gp firestore databases create --database="$FIRESTORE_DATABASE" --location="$REGION" \
    --type=firestore-native >/dev/null
  info "created database $FIRESTORE_DATABASE in $REGION"
fi
for group in $TOKEN_COLLECTIONS; do
  state="$(gp firestore indexes fields describe expires --collection-group="$group" \
    --database="$FIRESTORE_DATABASE" --format='value(ttlConfig.state)' 2>/dev/null || true)"
  [[ -n $state ]] && continue
  gp firestore fields ttls update expires --collection-group="$group" \
    --database="$FIRESTORE_DATABASE" --enable-ttl --async >/dev/null
done
info "TTL policies expire every token collection"

# -- client secret -----------------------------------------------------------

step "Client secret"

if ! gp secrets describe "$SECRET_NAME" >/dev/null 2>&1; then
  gp secrets create "$SECRET_NAME" --replication-policy=automatic >/dev/null
fi
STORED_SECRET="$(gp secrets versions access latest --secret="$SECRET_NAME" 2>/dev/null || true)"
if [[ $STORED_SECRET == "$CLIENT_SECRET" ]]; then
  info "$SECRET_NAME is current"
else
  printf '%s' "$CLIENT_SECRET" \
    | gp secrets versions add "$SECRET_NAME" --data-file=- >/dev/null
  info "stored a new version of $SECRET_NAME"
fi
unset STORED_SECRET

# -- service accounts --------------------------------------------------------

step "Service accounts"

RUN_SA="$(ensure_service_account "$SERVICE-run" "$APP server")"
BUILD_SA="$(ensure_service_account "$SERVICE-build" "$APP image builds")"
# The server reads and writes Firestore and reads its one secret; nothing else.
grant_on_project "serviceAccount:$RUN_SA" roles/datastore.user
retry gp secrets add-iam-policy-binding "$SECRET_NAME" \
  --member="serviceAccount:$RUN_SA" --role=roles/secretmanager.secretAccessor >/dev/null
# A dedicated builder, rather than the Compute Engine default account that new
# projects do not have until the Compute API is enabled.
grant_on_project "serviceAccount:$BUILD_SA" roles/cloudbuild.builds.builder
info "$RUN_SA: Firestore and $SECRET_NAME"
info "$BUILD_SA: Cloud Build"

# -- image -------------------------------------------------------------------

step "Building the image"

if ! gp artifacts repositories describe "$REPOSITORY" --location="$REGION" >/dev/null 2>&1; then
  gp artifacts repositories create "$REPOSITORY" --repository-format=docker \
    --location="$REGION" --description="$APP server images" >/dev/null
fi
cat > "$WORK/cleanup.json" <<'JSON'
[
  {"name": "keep-newest", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 3}},
  {"name": "delete-rest", "action": {"type": "Delete"}, "condition": {"tagState": "any"}}
]
JSON
# Without --dry-run, the policies delete old images for real.
gp artifacts repositories set-cleanup-policies "$REPOSITORY" --location="$REGION" \
  --policy="$WORK/cleanup.json" >/dev/null

TAG="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
[[ -z "$(git -C "$REPO_ROOT" status --porcelain)" ]] || TAG="$TAG-dirty"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPOSITORY/server:$TAG"
# A build under its own service account must log to Cloud Logging only.
cat > "$WORK/cloudbuild.yaml" <<'YAML'
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "--tag", "$_IMAGE", "."]
images: ["$_IMAGE"]
options:
  logging: CLOUD_LOGGING_ONLY
YAML
submit_build() {
  gp builds submit "$REPO_ROOT" --region="$REGION" --config="$WORK/cloudbuild.yaml" \
    --substitutions="_IMAGE=$IMAGE" \
    --service-account="projects/$PROJECT/serviceAccounts/$BUILD_SA" --suppress-logs
}

info "building $IMAGE (a few minutes)"
if ! submit_build; then
  # The first build in a new project can be refused while permission to act as
  # the builder account is still spreading; that clears in well under a minute.
  note "the build was refused; waiting for the builder's permissions, then trying once more"
  sleep 30
  submit_build || die "the build failed; see: gcloud builds list --region $REGION --project $PROJECT"
fi
info "built"

# -- custom domain, when the service exists ----------------------------------

SERVICE_EXISTS=0
gp run services describe "$SERVICE" --region="$REGION" >/dev/null 2>&1 && SERVICE_EXISTS=1

# A mapping names its service, which a first run has not created yet. When the
# service exists, it is mapped before the deploy, so a domain Google refuses
# stops the run before the public URL moves to an address that cannot answer.
if [[ -n $DOMAIN ]] && (( SERVICE_EXISTS )); then
  step "Custom domain"
  ensure_domain_mapping
fi

# -- deploy ------------------------------------------------------------------

step "Deploying to Cloud Run"

# A file rather than --set-env-vars, whose commas would split the allow lists.
# Every value was validated above to hold no quote or backslash.
cat > "$WORK/env.yaml" <<YAML
${PREFIX}_PUBLIC_URL: "$PUBLIC_URL"
${PREFIX}_GOOGLE_CLIENT_ID: "$CLIENT_ID"
${PREFIX}_ALLOWED_EMAILS: "$EMAILS"
${PREFIX}_ALLOWED_DOMAINS: "$DOMAINS"
${PREFIX}_ALLOW_ANY_GOOGLE_ACCOUNT: "$ALLOW_ANY"
${PREFIX}_TOKEN_STORE: "firestore"
${PREFIX}_FIRESTORE_DATABASE: "$FIRESTORE_DATABASE"
${PREFIX}_STATELESS_HTTP: "true"
YAML
# --allow-unauthenticated lets requests reach the app; the app's own OAuth
# refuses every $MCP_PATH call that carries no token it issued.
gp run deploy "$SERVICE" --region="$REGION" --image="$IMAGE" \
  --service-account="$RUN_SA" --allow-unauthenticated \
  --min-instances=0 --max-instances="$MAX_INSTANCES" --concurrency=80 \
  --cpu=1 --memory=512Mi --cpu-boost --timeout=300 --port=8080 \
  --env-vars-file="$WORK/env.yaml" \
  --set-secrets="${PREFIX}_GOOGLE_CLIENT_SECRET=$SECRET_NAME:latest" \
  --labels="$LABEL_KEY=$LABEL_VALUE" >/dev/null
info "deployed $SERVICE, up to $MAX_INSTANCES instance(s)"

if [[ -n $DOMAIN ]] && (( ! SERVICE_EXISTS )); then
  step "Custom domain"
  ensure_domain_mapping
fi

# -- budget and spend cap ----------------------------------------------------

step "Budget"

BUDGET_NAME="$APP: $PROJECT"
TOPIC_ID="$SERVICE-budget"
TOPIC="projects/$PROJECT/topics/$TOPIC_ID"
FUNCTION="$SERVICE-spend-cap"

if [[ $SHARED_BUDGET == true ]]; then
  # Another deployment in this project owns the budget and the kill switch, and
  # both are project-wide: the budget measures every server's spend together and
  # the function unlinks the whole project's billing. Creating a second pair here
  # would duplicate the alerts and measure exactly the same money twice.
  info "this project's cap is shared; the mcp-servers-shared-cap repo owns it"
  note "no budget or spend cap is created for $APP"
elif [[ $SPEND_CAP == true ]]; then
  if ! gp pubsub topics describe "$TOPIC_ID" >/dev/null 2>&1; then
    gp pubsub topics create "$TOPIC_ID" >/dev/null
  fi
  retry gp pubsub topics add-iam-policy-binding "$TOPIC_ID" \
    --member="serviceAccount:$BUDGET_PUBLISHER" --role=roles/pubsub.publisher >/dev/null

  SPEND_SA="$(ensure_service_account "$SERVICE-spendcap" "$APP spend cap")"
  # Removing this project's billing link and nothing more: Project Billing
  # Manager on this project, not admin over the whole billing account.
  grant_on_project "serviceAccount:$SPEND_SA" roles/billing.projectManager
  # Only projects created before April 2021 need this for Pub/Sub push, and a
  # new project's Pub/Sub service agent may not exist yet, so it is best effort.
  gp projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com" \
    --role=roles/iam.serviceAccountTokenCreator --condition=None >/dev/null 2>&1 || true

  [[ -d $SPEND_CAP_SOURCE ]] || die \
    "no spend cap function source at $SPEND_CAP_SOURCE. Check out the \
mcp-servers-shared-cap repo beside this one, set SPEND_CAP_SOURCE, or use the \
shared cap with ${PREFIX}_SHARED_BUDGET=true."
  info "deploying the $FUNCTION function (a few minutes)"
  retry gp functions deploy "$FUNCTION" --gen2 --region="$REGION" --runtime=python312 \
    --source="$SPEND_CAP_SOURCE" --entry-point=stop_billing \
    --trigger-topic="$TOPIC_ID" \
    --run-service-account="$SPEND_SA" --trigger-service-account="$SPEND_SA" \
    --build-service-account="projects/$PROJECT/serviceAccounts/$BUILD_SA" \
    --set-env-vars="SPEND_CAP_PROJECT=$PROJECT,SPEND_CAP_AT=$SPEND_CAP_AT,SPEND_CAP_DRY_RUN=$SPEND_CAP_DRY_RUN" \
    --max-instances=1 --memory=256Mi --timeout=60s --no-allow-unauthenticated >/dev/null
  retry gp run services add-iam-policy-binding "$FUNCTION" --region="$REGION" \
    --member="serviceAccount:$SPEND_SA" --role=roles/run.invoker >/dev/null
  info "$FUNCTION unlinks billing at ${SPEND_CAP_AT} of the budget (dry run: $SPEND_CAP_DRY_RUN)"
fi

# Replaced on every run rather than updated in place: creation's threshold
# flags are the unambiguously documented ones (fractions, 0.5 = 50%), and a
# fresh budget also stops feeding the spend cap's topic once the cap is off.
# Only this script's budget is touched, matched by its display name.
if [[ $SHARED_BUDGET == false ]]; then
  for budget_id in $(gcloud billing budgets list --billing-account="$BILLING" \
      --billing-project="$PROJECT" --filter="displayName=\"$BUDGET_NAME\"" \
      --format='value(name.basename())'); do
    gcloud billing budgets delete "$budget_id" --billing-account="$BILLING" \
      --billing-project="$PROJECT" --quiet >/dev/null
  done

  BUDGET_ARGS=(
    --billing-account="$BILLING" --billing-project="$PROJECT" --display-name="$BUDGET_NAME"
    --budget-amount="${BUDGET_USD}USD" --calendar-period=month
    --filter-projects="projects/$PROJECT_NUMBER"
    --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
  )
  [[ $SPEND_CAP == true ]] && BUDGET_ARGS+=(--notifications-rule-pubsub-topic="$TOPIC")
  gcloud billing budgets create "${BUDGET_ARGS[@]}" --quiet >/dev/null
  info "\$$BUDGET_USD monthly budget in place; alerts go to the billing account's admins"
fi

if [[ $SPEND_CAP == false ]] && gp functions describe "$FUNCTION" --region="$REGION" >/dev/null 2>&1; then
  warn "the spend cap is off; the $FUNCTION function no longer receives budget updates"
  note "remove it with: gcloud functions delete $FUNCTION --region $REGION --project $PROJECT"
fi

# -- verify ------------------------------------------------------------------

step "Verifying"

# Checked through the run.app URL, which answers as soon as the service is up,
# even while a custom domain waits for DNS and its certificate. Both checks hold
# there: discovery is served at any host name, and the token check comes before
# the host-name check that confines MCP calls to the public URL.
DISCOVERY=""
for _ in $(seq 1 24); do
  if DISCOVERY="$(curl -fsS --max-time 10 \
      "$RUN_URL/.well-known/oauth-protected-resource$MCP_PATH" 2>/dev/null)"; then
    break
  fi
  DISCOVERY=""
  sleep 5
done
[[ -n $DISCOVERY ]] || die "no answer from $RUN_URL
    logs: $LOGS_CMD"

RESOURCE="$(printf '%s' "$DISCOVERY" | sed -n 's/.*"resource" *: *"\([^"]*\)".*/\1/p')"
[[ ${RESOURCE%/} == "$PUBLIC_URL$MCP_PATH" ]] \
  || die "the server advertises ${RESOURCE:-nothing}, expected $PUBLIC_URL$MCP_PATH"
info "reachable from the internet, and the discovery document is correct"

STATUS="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST "$RUN_URL$MCP_PATH" \
  -H 'accept: application/json, text/event-stream' -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')"
[[ $STATUS == 401 ]] || die "an anonymous call returned HTTP $STATUS, expected 401"
info "anonymous calls are refused (401), as they should be"

DOMAIN_LIVE=0
if [[ -n $DOMAIN ]]; then
  if curl -fsS --max-time 10 -o /dev/null \
      "$PUBLIC_URL/.well-known/oauth-protected-resource$MCP_PATH" 2>/dev/null; then
    DOMAIN_LIVE=1
    info "$DOMAIN answers, with a valid certificate"
  else
    warn "$DOMAIN does not answer yet: add the DNS records below"
  fi
fi

cat <<EOF

${GRN}==>${RST} ${B}Done.${RST}

    The MCP endpoint:

      ${B}$PUBLIC_URL$MCP_PATH${RST}

    Claude:       Settings -> Connectors -> Add custom connector, with that URL
    Claude Code:  claude mcp add --transport http $APP $PUBLIC_URL$MCP_PATH

    Sign-in fails until the OAuth client lists this redirect URI:
      $REDIRECT_URI
EOF
if [[ -n $DOMAIN ]]; then
  [[ -n $DOMAIN_RECORDS ]] \
    || DOMAIN_RECORDS="(not reported yet; see Cloud Run -> Domain mappings in the console)"
  cat <<EOF

    DNS for $DOMAIN, at your DNS host (on Cloudflare: DNS only, not proxied):
$(printf '%s\n' "$DOMAIN_RECORDS" | sed 's/^/      /')
EOF
  (( DOMAIN_LIVE )) || cat <<EOF
    Google issues the certificate once the record resolves, which takes from
    about 15 minutes to a day. Until then the domain does not answer.
EOF
fi
if [[ $ALLOW_ANY == true ]]; then
  cat <<EOF
    Any Google account may sign in, but only once the app is published
    (Audience -> Publish app); in Testing, Google admits test users only:
      https://console.cloud.google.com/auth/audience?project=$PROJECT
EOF
fi
cat <<EOF

    ${DIM}logs      $LOGS_CMD${RST}
    ${DIM}update    $SCRIPT_DIR/deploy-gcp.sh${RST}
    ${DIM}remove    $SCRIPT_DIR/deploy-gcp.sh --teardown${RST}

EOF
