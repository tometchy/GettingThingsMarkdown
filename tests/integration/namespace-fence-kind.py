#!/usr/bin/env python3
"""Real kind admission checks on disposable namespaces only."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server/integrity"), str(ROOT / "server/storage-controller")]
import namespace_fence as f
import recovery_witness as w
import writer_fence
import witness_api
from recovery_crypto import RecoveryError
from kube_client import Kube

POLICIES = ["gtmd-writer-freeze-marker", "gtmd-frozen-namespace-no-writers"]


def uid():
    return str(uuid.uuid4())


def run(*args, data=None, check=True):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace"))
    return result


def k(*args, **kwargs):
    return run("kubectl", *args, **kwargs)


def get(*args):
    return json.loads(k(*args, "-o", "json").stdout)


def create(obj):
    k("create", "-f", "-", data=json.dumps(obj).encode())


def denied(args, message, data=None, eventually=False):
    deadline = time.monotonic() + (30 if eventually else 0)
    while True:
        res = k(*args, data=data, check=False)
        err = res.stderr.decode(errors="replace")
        if res.returncode and message in err:
            return
        if time.monotonic() >= deadline:
            raise AssertionError("Expected exact admission rejection: " + message + "; got " + err)
        time.sleep(.5)


def pod(ns, image, name="probe"):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": ns},
            "spec": {"containers": [{"name": "writer", "image": image, "imagePullPolicy": "Never"}]}}


def keypair(root, name):
    key, pub = root / (name + ".key"), root / (name + ".pub")
    run("openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key))
    run("openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub))
    return key, pub


def main():
    ns, other = ["gtm-" + uuid.uuid4().hex for _ in range(2)]
    role = ns + "-fence"
    actor = "system:serviceaccount:gtm-system:" + role
    try:
        rendered = run("helm", "template", "gtm", str(ROOT / "infrastructure/helm/gtm-controlplane"),
                       "--show-only", "templates/writer-fence-admission.yaml",
                       "--set", "writerFence.enabled=true").stdout
        k("apply", "-f", "-", data=rendered)
        for n in (ns, other):
            k("create", "namespace", n)
        k("apply", "-f", "-", data=json.dumps(f.actor_manifest(ns)).encode())
        assert k("auth", "can-i", "patch", "namespace/" + other, "--as=" + actor,
                 check=False).stdout.strip() == b"no"
        assert k("auth", "can-i", "patch", "validatingadmissionpolicy/" + POLICIES[0],
                 "--as=" + actor, check=False).stdout.strip() == b"no"
        assert k("auth", "can-i", "delete", "pod", "-n", ns, "--as=" + actor,
                 check=False).stdout.strip() == b"no"
        node = get("get", "nodes")["items"][0]
        node_name = node["metadata"]["name"]
        images = json.loads(run("docker", "exec", node_name, "crictl", "images", "-o", "json").stdout)
        image = next(t for i in images["images"] for t in i.get("repoTags", []) if "/pause:" in t)
        spec = pod(ns, image)["spec"]
        spec["nodeName"] = node_name
        labels = {"app": "ci-writer"}
        create({"apiVersion": "apps/v1", "kind": "StatefulSet",
                "metadata": {"name": "sync", "namespace": ns},
                "spec": {"serviceName": "sync", "replicas": 1, "selector": {"matchLabels": labels},
                         "template": {"metadata": {"labels": labels}, "spec": spec}}})
        k("-n", ns, "wait", "--for=condition=Ready", "pod/sync-0", "--timeout=90s")
        pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
               "metadata": {"name": "sync-data", "namespace": ns},
               "spec": {"storageClassName": "", "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": "1Gi"}}}}
        create(pvc)
        ids = {"podUid": get("-n", ns, "get", "pod", "sync-0")["metadata"]["uid"],
               "nodeUid": node["metadata"]["uid"],
               "pvcUids": [get("-n", ns, "get", "pvc", "sync-data")["metadata"]["uid"]]}
        cfg = {"tenant": ns, "namespaceUid": get("get", "ns", ns)["metadata"]["uid"],
               "role": "sync", "identity": ids}
        # Real witness state machine, signed challenge, API dispatch; mTLS
        # transport is exercised separately by witness-api.py.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            key, pub = keypair(root, "witness")
            roles = {}
            for name in ("sync", "history"):
                _, p = keypair(root, name)
                roles[name] = {"cloud": name, "publicKey": str(p),
                               "sourceUrl": "https://" + name + ".example.invalid/repo.git"}
            config = root / "config.json"
            config.write_text(json.dumps({"tenants": {ns: roles}}))
            witness = w.Witness(root / "state", w.config(config, key), key)
            witness.initialize(ns)
            writer_fence.enroll(witness, ns, {"sync": ids, "history":
                                {"podUid": uid(), "nodeUid": uid(), "pvcUids": [uid()]}})
            class Client:
                def call(self, request):
                    return witness_api.dispatch(witness, request)
            # Before freeze, a normal pod admission succeeds and even admin
            # cannot install the protected marker.
            k("create", "--dry-run=server", "-f", "-", data=json.dumps(pod(ns, image)).encode())
            denied(["annotate", "ns", ns, f.MARKER + "=" + "a"*64, "--dry-run=server"],
                   "GTMD freeze marker may only be installed by the dedicated actor", eventually=True)
            writer_fence.freeze(witness, ns, "ci-incident")
            kubeconfig = json.loads(k("config", "view", "--raw", "--minify", "-o", "json").stdout)
            cluster = kubeconfig["clusters"][0]["cluster"]
            ca, token = root / "ca", root / "token"
            ca.write_bytes(base64.b64decode(cluster["certificate-authority-data"]))
            token.write_bytes(k("-n", "gtm-system", "create", "token", role,
                                "--duration=10m").stdout)
            api = Kube(cluster["server"], str(ca), str(token))
            try:
                first = f.reconcile(api, Client(), pub, cfg, apply=True)
                assert first["state"] == "marker_present"
                assert first == f.reconcile(api, Client(), pub, cfg, apply=True)
            except RecoveryError:
                # Show type-check warnings on real API; never fall back to
                # bypassing the policy or granting broader permissions.
                print(k("get", "validatingadmissionpolicy", *POLICIES, "-o", "yaml").stdout.decode())
                raise
        reason = "GTMD tenant namespace is frozen; writer admission denied"
        probe = pod(ns, image, "other-writer")
        probe["spec"]["volumes"] = [{"name": "old", "persistentVolumeClaim": {"claimName": "sync-data"}}]
        probe["spec"]["containers"][0]["volumeMounts"] = [{"name": "old", "mountPath": "/data"}]
        denied(["create", "--dry-run=server", "-f", "-"], reason, json.dumps(probe).encode(), eventually=True)
        denied(["-n", ns, "exec", "sync-0", "--", "/not-executed"], reason)
        denied(["-n", ns, "annotate", "pvc", "sync-data", "ci-change=yes", "--dry-run=server"], reason)
        denied(["-n", ns, "delete", "pvc", "sync-data", "--dry-run=server"], reason)
        new_pvc = json.loads(json.dumps(pvc))
        new_pvc["metadata"]["name"] = "replacement"
        denied(["create", "--dry-run=server", "-f", "-"], reason, json.dumps(new_pvc).encode())
        denied(["annotate", "ns", ns, f.MARKER + "-", "--dry-run=server"],
               "GTMD frozen namespace marker is immutable")
        denied(["--as=" + actor, "annotate", "ns", ns, "unexpected=yes", "--dry-run=server"],
               "GTMD freeze actor may only add the immutable freeze marker")
        denied(["delete", "ns", ns, "--dry-run=server"], "GTMD frozen namespace marker is immutable")
        # Actual StatefulSet replacement attempt after removing the old pod.
        k("-n", ns, "delete", "pod", "sync-0", "--wait=true", "--timeout=60s")
        deadline = time.monotonic() + 30
        while True:
            events = get("-n", ns, "get", "events")["items"]
            if any(reason in e.get("message", "") and e.get("reason") == "FailedCreate" for e in events):
                break
            if time.monotonic() >= deadline:
                raise AssertionError("StatefulSet did not report exact rejected replacement")
            time.sleep(1)
        assert not get("-n", ns, "get", "pods")["items"]
        assert get("-n", ns, "get", "pvc", "sync-data")["metadata"]["uid"] == ids["pvcUids"][0]
        k("create", "--dry-run=server", "-f", "-", data=json.dumps(pod(other, image)).encode())
        print("PASS: signed namespace freeze, real API admission, scoped RBAC, immutable marker, StatefulSet replacement denied; unrelated tenant unaffected.")
    finally:
        # Explicitly disposable CI cluster only. Never uninstall the barrier
        # to unfreeze a real tenant.
        k("delete", "validatingadmissionpolicybinding", *POLICIES, "--ignore-not-found", check=False)
        k("delete", "validatingadmissionpolicy", *POLICIES, "--ignore-not-found", check=False)
        k("delete", "clusterrolebinding", role, "--ignore-not-found", check=False)
        k("delete", "clusterrole", role, "--ignore-not-found", check=False)
        k("-n", "gtm-system", "delete", "serviceaccount", role, "--ignore-not-found", check=False)
        k("delete", "ns", ns, other, "--ignore-not-found", "--wait=false", check=False)


if __name__ == "__main__":
    main()
