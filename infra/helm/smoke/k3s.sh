#!/usr/bin/env bash
# Install the chart on a throwaway k3s cluster in Docker and check it end to
# end: migration hook, helm test, Ingress → nginx → API → DB/Redis, admin
# login, an upgrade that rolls the pods, worker liveness. Needs only Docker
# (kubectl on the host is optional); runs k3s as a *privileged* container.
#
#   infra/helm/smoke/k3s.sh            # build nothing, use the local images
#   KEEP=1 infra/helm/smoke/k3s.sh     # leave the cluster up afterwards
#
# Images: annotation-{backend,frontend,model}:local (`docker compose build`).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
charts=$(cd "$here/.." && pwd)
work=$(mktemp -d)
name=ap-k3s
net=ap-k3s-net
port=${K3S_INGRESS_PORT:-18090}

cleanup() {
  if [[ -z "${KEEP:-}" ]]; then
    # -v: the k3s image declares volumes (its image store among them);
    # without it every run leaves gigabytes behind.
    docker rm -f -v "$name" >/dev/null 2>&1 || true
    docker network rm "$net" >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

k() { docker exec -i "$name" kubectl "$@"; }
helm() {
  docker run --rm --network "$net" -v "$work/kubeconfig":/kube/config:ro -e KUBECONFIG=/kube/config \
    -v "$charts":/charts -v "$here":/smoke -w /charts alpine/helm:3.16.3 "$@"
}

echo "==> k3s"
docker network create "$net" >/dev/null 2>&1 || true
docker run -d --name "$name" --hostname "$name" --network "$net" --privileged -p "$port:80" \
  rancher/k3s:v1.30.6-k3s1 server --tls-san "$name" --disable metrics-server \
  --kubelet-arg=eviction-hard=imagefs.available\<2%,nodefs.available\<2% \
  --kubelet-arg=image-gc-high-threshold=99 --kubelet-arg=image-gc-low-threshold=98 >/dev/null
# (A throwaway node on a developer's Docker disk: the default 85% thresholds
# would taint it with disk-pressure before anything is scheduled.)
for _ in $(seq 1 90); do k get nodes 2>/dev/null | grep -q " Ready" && break; sleep 2; done
docker exec "$name" cat /etc/rancher/k3s/k3s.yaml | sed "s#https://127.0.0.1:6443#https://$name:6443#" > "$work/kubeconfig"
chmod 600 "$work/kubeconfig"

echo "==> images"
arch=$(docker version --format '{{.Server.Arch}}')
# The compose stack's postgres and redis images too: no registry pulls needed.
for image in annotide-backend:local annotide-frontend:local annotide-model:local \
  postgres:16-alpine redis:7-alpine; do
  # One platform only: a multi-platform save lists layers it does not carry.
  docker save --platform "linux/$arch" "$image" | docker exec -i "$name" ctr -n k8s.io images import - >/dev/null
done

echo "==> throwaway postgres + redis"
k apply -f - < "$here/deps.yaml" >/dev/null
k -n annotate rollout status deploy/postgres --timeout=300s
k -n annotate rollout status deploy/redis --timeout=120s

echo "==> install"
helm upgrade --install annotate annotide -n annotate -f /smoke/values.yaml --wait --timeout 6m >/dev/null
helm test annotate -n annotate | grep -q "Phase:          Succeeded"
curl -fsS -H "Host: annotate.localtest" "http://localhost:$port/api/v1/ready" | grep -q '"status":"ready"'
k -n annotate exec deploy/annotate-annotide-backend -- \
  python -m app.cli create-superuser --email smoke@example.com --password smoke-password >/dev/null
curl -fsS -H "Host: annotate.localtest" -H "Content-Type: application/json" \
  -d '{"email":"smoke@example.com","password":"smoke-password"}' \
  "http://localhost:$port/api/v1/auth/login" | grep -q access_token

echo "==> upgrade"
before=$(k -n annotate get pods -l app.kubernetes.io/component=backend -o name | sort)
helm upgrade annotate annotide -n annotate -f /smoke/values.yaml \
  --set config.APP_LOG_LEVEL=DEBUG --wait --timeout 5m >/dev/null
after=$(k -n annotate get pods -l app.kubernetes.io/component=backend -o name | sort)
[[ "$before" != "$after" ]] || { echo "config change did not roll the backend"; exit 1; }
if k -n annotate get secret -o name | grep -q migrate-env; then
  echo "migration hook secret was left behind"; exit 1
fi

echo "==> worker liveness (70 s)"
sleep 70
restarts=$(k -n annotate get pods -l app.kubernetes.io/component=worker \
  -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')
[[ "$restarts" == "0" ]] || { echo "worker restarted $restarts times"; exit 1; }

echo "OK: chart installs, tests, serves, upgrades and stays healthy on k3s"
