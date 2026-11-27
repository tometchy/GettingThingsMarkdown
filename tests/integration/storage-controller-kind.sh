#!/usr/bin/env bash
# Real API server and PV binder; no actual tenant files or physical mount.
set -euo pipefail
cd "$(dirname "$0")/../.."
tmp="$(mktemp -d)"
ns=gtm-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
class=gtmd-ci-local
cleanup() {
  rc=$?
  if [ "$rc" != 0 ]; then
    kubectl -n gtm-system logs deployment/storage-controller --tail=100 || true
    kubectl get pv ci-gtmd-free ci-gtmd-bound ci-gtmd-healthy -o yaml || true
    kubectl get validatingadmissionpolicy gtmd-storage-reservation-only -o yaml || true
    if [ -f "$tmp/denial" ]; then cat "$tmp/denial"; fi
  fi
  kubectl -n gtm-system delete deployment storage-controller --ignore-not-found >/dev/null 2>&1 || true
  kubectl delete validatingadmissionpolicybinding gtmd-storage-reservation-only gtmd-quarantine-no-pvcs --ignore-not-found >/dev/null 2>&1 || true
  kubectl delete validatingadmissionpolicy gtmd-storage-reservation-only gtmd-quarantine-no-pvcs --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n gtm-system delete configmap ci-storage-inventory --ignore-not-found >/dev/null 2>&1 || true
  kubectl delete namespace "$ns" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl delete pv ci-gtmd-free ci-gtmd-bound ci-gtmd-healthy --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl delete storageclass "$class" --ignore-not-found >/dev/null 2>&1 || true
  rm -rf "$tmp"
}
trap cleanup EXIT

# Use an actual Ready node and its UID. No host device access is required.
kubectl get nodes -o json > "$tmp/nodes.json"
python3 - "$tmp" "$ns" "$class" <<'PY'
import json, pathlib, sys
p, ns, sc = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
node = json.loads((p/"nodes.json").read_text())["items"][0]["metadata"]
name, uid, hostname = node["name"], node["uid"], node["labels"]["kubernetes.io/hostname"]
bad, good = "a"*24, "b"*24
def meta(n, namespace=None):
    return {"name":n, **({"namespace":namespace} if namespace else {})}
def obj(api, kind, n, namespace=None, **data):
    return {"apiVersion":api,"kind":kind,"metadata":meta(n,namespace),**data}
def pv(n,disk):
    root = "/tmp/gtmd-ci-bad" if disk == bad else "/tmp/gtmd-ci-good"
    v = obj("v1","PersistentVolume",n,spec={"storageClassName":sc,"capacity":{"storage":"1Gi"},
       "accessModes":["ReadWriteOnce"],"persistentVolumeReclaimPolicy":"Retain",
       "local":{"path":root+"/"+n},
       "nodeAffinity":{"required":{"nodeSelectorTerms":[{"matchExpressions":[
           {"key":"kubernetes.io/hostname","operator":"In","values":[hostname]}]}]}}})
    v["metadata"]["annotations"]={"gtmd.io/node":name,"gtmd.io/node-uid":uid,"gtmd.io/disk-id":disk}
    v["metadata"]["labels"]={"gtmd-ci-volume":n}
    return v
def pvc(n, vol):
    return obj("v1","PersistentVolumeClaim",n,ns,spec={"storageClassName":sc,
        "accessModes":["ReadWriteOnce"],"resources":{"requests":{"storage":"1Gi"}},
        "selector":{"matchLabels":{"gtmd-ci-volume":vol}}})
inventory={"storageClasses":[sc],"nodes":[{"name":name,"uid":uid,"hostname":hostname,"cloud":"ci-a",
           "disks":[{"id":bad,"mount":"/tmp/gtmd-ci-bad"},{"id":good,"mount":"/tmp/gtmd-ci-good"}]}]}
base=[obj("v1","Namespace",ns),obj("storage.k8s.io/v1","StorageClass",sc,
      provisioner="kubernetes.io/no-provisioner",volumeBindingMode="Immediate"),
      pv("ci-gtmd-free",bad),pv("ci-gtmd-bound",bad),pv("ci-gtmd-healthy",good),
      pvc("sync-data","ci-gtmd-bound"),
      obj("v1","ConfigMap","ci-storage-inventory","gtm-system",data={"inventory.json":json.dumps(inventory)})]
report=obj("v1","ConfigMap",name,"gtm-storage-reports",data={"report.json":"{}"})
report["metadata"]["annotations"]={"gtmd.io/node-uid":uid}
sa=obj("v1","ServiceAccount","ci-storage-reporter","gtm-storage-reports")
role=obj("rbac.authorization.k8s.io/v1","Role","ci-storage-reporter","gtm-storage-reports",
        rules=[{"apiGroups":[""],"resources":["configmaps"],"resourceNames":[name],"verbs":["get","patch"]}])
rb=obj("rbac.authorization.k8s.io/v1","RoleBinding","ci-storage-reporter","gtm-storage-reports",
       roleRef={"apiGroup":"rbac.authorization.k8s.io","kind":"Role","name":"ci-storage-reporter"},
       subjects=[{"kind":"ServiceAccount","name":"ci-storage-reporter","namespace":"gtm-storage-reports"}])
def save(n, items):
    (p/n).write_text(json.dumps({"apiVersion":"v1","kind":"List","items":items}))
save("base.json",base)
save("report.json",[report,sa,role,rb])
save("pending.json",[pvc("should-stay-pending","ci-gtmd-free")])
(p/"node").write_text(name)
(p/"uid").write_text(uid)
PY
kubectl apply -f "$tmp/base.json"
kubectl -n "$ns" wait --for=jsonpath='{.status.phase}'=Bound pvc/sync-data --timeout=90s
bound_uid="$(kubectl get pv ci-gtmd-bound -o jsonpath='{.spec.claimRef.uid}')"
test -n "$bound_uid"

# Controller starts only after scoped reporter and independent inventory exist.
kubectl create namespace gtm-storage-reports --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$tmp/report.json"
kubectl -n gtm-storage-reports create token ci-storage-reporter --duration=10m > "$tmp/token"
chmod 600 "$tmp/token"
kubectl config view --raw --minify -o json > "$tmp/kubeconfig.json"
python3 - "$tmp" <<'PY'
import base64, json, pathlib, sys, time
p=pathlib.Path(sys.argv[1])
cluster=json.loads((p/"kubeconfig.json").read_text())["clusters"][0]["cluster"]
(p/"ca").write_bytes(base64.b64decode(cluster["certificate-authority-data"]))
(p/"endpoint").write_text(cluster["server"])
now=time.time()
(p/"health.json").write_text(json.dumps({"inventoryOk":True,"checkedUnix":now,"disks":[
    {"id":"a"*24,"state":"suspect","checkedUnix":now},{"id":"b"*24,"state":"healthy","checkedUnix":now}]}))
PY
python3 server/storage-controller/publish_report.py --node "$(cat "$tmp/node")" \
  --uid "$(cat "$tmp/uid")" --endpoint "$(cat "$tmp/endpoint")" --ca "$tmp/ca" \
  --token "$tmp/token" --report "$tmp/health.json"
test "$(kubectl auth can-i patch pv --as=system:serviceaccount:gtm-storage-reports:ci-storage-reporter)" = no
test "$(kubectl auth can-i patch configmap/other --namespace=gtm-storage-reports --as=system:serviceaccount:gtm-storage-reports:ci-storage-reporter)" = no

# Render the actual optional Helm component; do not replace the control plane.
helm template gtm infrastructure/helm/gtm-controlplane \
  --show-only templates/storage-controller.yaml \
  --show-only templates/storage-admission.yaml \
  --set storageController.enabled=true \
  --set storageController.image=gtm/storage-controller:ci \
  --set storageController.inventoryConfigMap=ci-storage-inventory \
  --set storageController.reserveAvailable=true \
  --set "storageController.managedClasses[0]=$class" | kubectl apply -f -
kubectl -n gtm-system rollout status deployment/storage-controller --timeout=120s
for _ in $(seq 1 30); do
  if [ "$(kubectl get pv ci-gtmd-free -o jsonpath='{.spec.claimRef.namespace}')" = gtm-storage-quarantine ]; then
    break
  fi
  sleep 3
done
if [ "$(kubectl get pv ci-gtmd-free -o jsonpath='{.spec.claimRef.namespace}')" != gtm-storage-quarantine ]; then
  echo "Expected free PV to be quarantined" >&2
  actor=system:serviceaccount:gtm-system:gtm-storage-controller
  kubectl --as="$actor" patch pv ci-gtmd-free --type=merge --dry-run=server \
    -p '{"spec":{"claimRef":{"apiVersion":"v1","kind":"PersistentVolumeClaim","namespace":"gtm-storage-quarantine","name":"quarantine-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}}' || true
  exit 1
fi
test "$(kubectl get pv ci-gtmd-bound -o jsonpath='{.spec.claimRef.uid}')" = "$bound_uid"
test -z "$(kubectl get pv ci-gtmd-healthy -o jsonpath='{.spec.claimRef.name}')"
kubectl -n gtm-system logs deployment/storage-controller > "$tmp/controller.log"
grep -q '"action":"repair_required"' "$tmp/controller.log"
kubectl apply -f "$tmp/pending.json"
sleep 8
test "$(kubectl -n "$ns" get pvc should-stay-pending -o jsonpath='{.status.phase}')" = Pending
# A compromised controller identity must not be able to change arbitrary PV fields.
actor=system:serviceaccount:gtm-system:gtm-storage-controller
if kubectl --as="$actor" patch pv ci-gtmd-healthy --type=merge \
  -p '{"metadata":{"labels":{"unexpected":"change"}}}' --dry-run=server > "$tmp/denial" 2>&1; then
  echo 'Admission policy unexpectedly allowed unrelated PV mutation' >&2
  exit 1
fi
grep -Eq 'Only the quarantine|Only available|Reservation must not change|denied' "$tmp/denial"
# Even an ordinary claim creator must not bind the quarantine reservation.
reserved="$(kubectl get pv ci-gtmd-free -o jsonpath='{.spec.claimRef.name}')"
cat <<EOF | kubectl create --dry-run=client -f - -o yaml > "$tmp/forbidden.yaml"
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $reserved
  namespace: gtm-storage-quarantine
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: $class
  resources:
    requests:
      storage: 1Gi
EOF
if kubectl create --dry-run=server -f "$tmp/forbidden.yaml" > "$tmp/denial" 2>&1; then
  echo 'Admission policy unexpectedly allowed a quarantine PVC' >&2
  exit 1
fi
grep -q 'Quarantine claim names are intentionally unbindable' "$tmp/denial"
echo 'PASS: real Kubernetes PV reservation prevents binding, bound claim unchanged, healthy free PV unchanged, reporter scoped, unsafe writes denied.'
