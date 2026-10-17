#!/bin/sh
# e2e.sh — end-to-end tests for install.sh against a real Docker daemon.
#
#   sh deploy/personal/tests/e2e.sh guard       # Postgres only, under a minute
#   sh deploy/personal/tests/e2e.sh reinstall   # the full stack, a few minutes
#
# guard: an earlier install's database volume left behind (with and without
# its containers) must stop install.sh before anything starts, and the
# command it prints must really delete the volume. Needs only
# postgres:16-alpine, never the Annotide images.
#
# reinstall: a real install, then a reinstall into a new directory, which
# must stop; then both ways out: the old .env restored (the old admin still
# signs in) and the old database deleted (a fresh install comes up). Needs
# the Annotide images: set ANNOTIDE_REGISTRY and ANNOTIDE_VERSION to local
# ones (CI builds them), or it pulls ghcr.io/annotide/*:latest.
#
# Everything runs under its own Compose project (annotide-e2e-*), so it never
# touches a real install's `annotide` volume, and is removed on exit.
# INSTALL_SH points at another install.sh, e.g. an older one to show that
# the test catches the bug it guards against.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
install_sh="${INSTALL_SH:-$here/../install.sh}"
compose_file="$here/../compose.yaml"

project="annotide-e2e-$(date +%s)-$$"
volume="${project}_postgres_data"
work=$(mktemp -d "${TMPDIR:-/tmp}/annotide-e2e.XXXXXX")
port="${E2E_PORT:-18080}"
admin_email="admin@e2e.example.com"

log() { printf '==> %s\n' "$*"; }
fail() {
    printf 'FAIL: %s\n' "$*" >&2
    for f in "$work"/*.out "$work"/*.err; do
        [ -s "$f" ] || continue
        printf -- '--- %s\n' "$f" >&2
        cat "$f" >&2
    done
    exit 1
}

project_containers() {
    docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.ID}} {{.State}}' | sort
}

cleanup() {
    status=$?
    filter="label=com.docker.compose.project=$project"
    docker ps -aq --filter "$filter" | xargs docker rm -f >/dev/null 2>&1 || true
    docker volume ls -q --filter "$filter" | xargs docker volume rm >/dev/null 2>&1 || true
    docker network ls -q --filter "$filter" | xargs docker network rm >/dev/null 2>&1 || true
    # ./data may hold files the containers wrote as root.
    rm -rf "$work" 2>/dev/null || true
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' HUP INT PIPE TERM

export COMPOSE_PROJECT_NAME="$project"

# run_install <name> [VAR=value...]: runs install.sh into $work/<name> without a
# terminal; output in $work/<name>.out and .err, exit status in $rc.
run_install() {
    name="$1"
    shift
    rc=0
    env ANNOTIDE_DIR="$work/$name" ANNOTIDE_COMPOSE_FILE="$compose_file" \
        ANNOTIDE_PORT="$port" "$@" \
        sh "$install_sh" </dev/null >"$work/$name.out" 2>"$work/$name.err" || rc=$?
}

# The `docker ...` line install.sh printed as the way to delete the volume.
printed_remove_command() {
    grep -E '^[[:space:]]*docker (rm|volume rm) ' "$1" | head -n 1 | sed 's/^[[:space:]]*//'
}

# expect_guard <name> <containers listed: yes|no>: the install just run into
# $work/<name> stopped on the leftover volume, started nothing, wrote no
# .env, and printed a delete command that works.
expect_guard() {
    name="$1"
    [ "$rc" -eq 1 ] || fail "$name: install.sh exited $rc, expected 1"
    grep -q "An earlier Annotide install left its database behind" "$work/$name.err" \
        || fail "$name: no leftover-database message"
    grep -q "docker volume rm $volume" "$work/$name.err" \
        || fail "$name: the message does not name $volume"
    [ ! -e "$work/$name/.env" ] || fail "$name: install.sh wrote a .env anyway"
    [ "$(project_containers)" = "$containers_before" ] \
        || fail "$name: containers changed: before [$containers_before], after [$(project_containers)]"
    if [ "$2" = yes ]; then
        grep -q "docker rm -f .*${project}-postgres-1" "$work/$name.err" \
            || fail "$name: the delete command does not remove the old containers"
    else
        if grep -q "docker rm -f" "$work/$name.err"; then
            fail "$name: names containers to remove, but there are none"
        fi
    fi
}

run_printed_remove_command() {
    cmd=$(printed_remove_command "$work/$1.err")
    [ -n "$cmd" ] || fail "$1: found no delete command in the message"
    log "Running the printed command: $cmd"
    sh -c "$cmd" >/dev/null || fail "$1: the printed delete command failed: $cmd"
    if docker volume inspect "$volume" >/dev/null 2>&1; then
        fail "$1: $volume still exists after the printed command"
    fi
}

# A database volume made exactly as a real install makes it: Postgres
# started through compose.yaml with an .env, so the volume and container
# carry Compose's names and labels.
seed_old_database() {
    old="$work/old"
    mkdir -p "$old"
    cp "$compose_file" "$old/compose.yaml"
    printf 'POSTGRES_PASSWORD=old-password\nAPP_SECRET_KEY=old\nAPP_INSTALL_ID=old\n' >"$old/.env"
    ( cd "$old" && docker compose up -d --wait postgres ) >/dev/null 2>&1 \
        || fail "could not start Postgres for the earlier install"
}

test_guard() {
    log "Leftover volume and containers, install directory deleted"
    seed_old_database
    rm -rf "$work/old"
    containers_before=$(project_containers)
    run_install with-containers
    expect_guard with-containers yes
    run_printed_remove_command with-containers

    log "Leftover volume only (docker compose down without -v)"
    seed_old_database
    ( cd "$work/old" && docker compose down ) >/dev/null 2>&1
    rm -rf "$work/old"
    containers_before=$(project_containers)
    run_install volume-only
    expect_guard volume-only no
    run_printed_remove_command volume-only
}

# sign_in <password>: true when $admin_email signs in with it.
sign_in() {
    body=$(printf '{"email":"%s","password":"%s"}' "$admin_email" "$1")
    curl -fsS -o /dev/null -H 'Content-Type: application/json' -d "$body" \
        "http://127.0.0.1:$port/api/v1/auth/login" 2>/dev/null
}

expect_running() {
    [ "$rc" -eq 0 ] || fail "$1: install.sh exited $rc, expected 0"
    curl -fsS -o /dev/null "http://127.0.0.1:$port/api/v1/health" \
        || fail "$1: the API is not answering on port $port"
}

test_reinstall() {
    log "First install"
    run_install first ADMIN_EMAIL="$admin_email" ADMIN_PASSWORD=first-password-1
    expect_running first
    sign_in first-password-1 || fail "first: the admin cannot sign in"
    cp "$work/first/.env" "$work/first.env"

    log "Reinstall into a new directory, the first stack still running"
    containers_before=$(project_containers)
    run_install second ADMIN_EMAIL="$admin_email" ADMIN_PASSWORD=second-password-2
    expect_guard second yes
    sign_in first-password-1 || fail "second: the first stack stopped serving"

    log "Way out 1: the earlier .env restored keeps the old data"
    cp "$work/first.env" "$work/second/.env"
    run_install second ADMIN_EMAIL="$admin_email" ADMIN_PASSWORD=second-password-2
    expect_running second
    sign_in first-password-1 || fail "second: the old admin cannot sign in with the old data"

    log "Way out 2: the printed command deletes the old database"
    containers_before=$(project_containers)
    run_install third ADMIN_EMAIL="$admin_email" ADMIN_PASSWORD=third-password-3
    expect_guard third yes
    run_printed_remove_command third
    run_install third ADMIN_EMAIL="$admin_email" ADMIN_PASSWORD=third-password-3
    expect_running third
    sign_in third-password-3 || fail "third: the new admin cannot sign in"
    if sign_in first-password-1; then
        fail "third: the old admin still signs in, so the old database survived"
    fi
}

case "${1:-}" in
    guard) test_guard ;;
    reinstall) test_reinstall ;;
    *)
        printf 'Usage: %s guard|reinstall\n' "$0" >&2
        exit 2
        ;;
esac
log "PASS: $1"
