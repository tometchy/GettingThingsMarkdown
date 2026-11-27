#!/usr/bin/env python3
"""Real static PV binder + actual witness signatures, no physical storage."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/"server/integrity"), str(ROOT/"server/storage-controller")]
import repair_allocator as a
import writer_fence
import recovery_witness as w
import witness_api
from recovery_crypto import digest, sign
from kube_client import Kube

CLASS = "gtmd-ci-repair"
NORMAL = "gtmd-ci-original"
POLICIES = ["gtmd-recovery-no-pods", "gtmd-recovery-claims", "gtmd-repair-pool-only"]
ACTOR = "system:serviceaccount:gtm-system:gtm-repair-allocator"


def run(*args, data=None, check=True):
    p = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if check and p.returncode:
        raise RuntimeError(p.stderr.decode(errors="replace"))
    return p


def k(*args, **kw):
    return run("kubectl", *args, **kw)


def get(*args):
    return json.loads(k(*args, "-o", "json").stdout)


def create(obj):
    k("create", "-f", "-", data=json.dumps(obj).encode())


def deny(args, reason, data=None):
    deadline = time.monotonic()+20
    while True:
        p = k(*args, data=data, check=False)
        if p.returncode and reason in p.stderr.decode(errors="replace"):
            return
        if time.monotonic() >= deadline:
            raise AssertionError(p.stderr.decode(errors="replace"))
        time.sleep(.5)


def uid():
    return str(uuid.uuid4())


def main():
    tenant = "gtm-"+uuid.uuid4().hex
    nodes = get("get", "nodes")["items"]
    assert len(nodes) >= 3
    inventory = {"storageClasses": [NORMAL, CLASS], "nodes": []}
    for i, n in enumerate(nodes[:3]):
        m = n["metadata"]
        inventory["nodes"].append({"name": m["name"], "uid": m["uid"],
                                   "hostname": m["labels"]["kubernetes.io/hostname"],
                                   "cloud": "a" if i < 2 else "b",
                                   "disks": [{"id": str(i+1)*24, "mount": "/tmp/gtmd-repair-ci-"+str(i)}]})
    ns = inventory["nodes"]
    cfg = a.inventory(inventory)
    names = ["ci-repair-old", "ci-repair-small", "ci-repair-fit", "ci-repair-large", "ci-repair-other"]

    def pv(name, i, cap, spare=True):
        n = ns[i]
        return {"apiVersion": "v1", "kind": "PersistentVolume",
                "metadata": {"name": name, "labels": {a.SPARE: "true"} if spare else {},
                             "annotations": {"gtmd.io/node": n["name"], "gtmd.io/node-uid": n["uid"],
                                             "gtmd.io/disk-id": n["disks"][0]["id"]}},
                "spec": {"storageClassName": CLASS if spare else NORMAL,
                         "capacity": {"storage": cap}, "accessModes": ["ReadWriteOnce"],
                         "persistentVolumeReclaimPolicy": "Retain",
                         "local": {"path": n["disks"][0]["mount"]+"/"+name},
                         "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                             {"key": "kubernetes.io/hostname", "operator": "In", "values": [n["hostname"]]}]}]}}}}

    def reports():
        for n in ns:
            disk = n["disks"][0]["id"]
            now = time.time()
            obj = {"apiVersion": "v1", "kind": "ConfigMap",
                   "metadata": {"namespace": "gtm-storage-reports", "name": n["name"],
                                "annotations": {"gtmd.io/node-uid": n["uid"]}},
                   "data": {"report.json": json.dumps({"node": n["name"], "nodeUid": n["uid"],
                             "inventoryOk": True, "checkedUnix": now,
                             "disks": [{"id": disk, "state": "healthy", "checkedUnix": now}]})}}
            k("apply", "-f", "-", data=json.dumps(obj).encode())

    try:
        k("create", "ns", tenant)
        k("create", "ns", "gtm-storage-reports", check=False)
        manifest = run("helm", "template", "gtm", str(ROOT/"infrastructure/helm/gtm-controlplane"),
                       "--show-only", "templates/repair-allocator.yaml",
                       "--set", "repairAllocator.enabled=true",
                       "--set", "repairAllocator.storageClass="+CLASS).stdout
        k("apply", "-f", "-", data=manifest)
        for cls in (NORMAL, CLASS):
            create({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
                    "metadata": {"name": cls}, "provisioner": "kubernetes.io/no-provisioner",
                    "volumeBindingMode": "Immediate"})
        create(pv(names[0],0,"10Gi",False))
        old = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
               "metadata": {"name": "sync-data", "namespace": tenant},
               "spec": {"storageClassName": NORMAL, "volumeName": names[0],
                        "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "10Gi"}}}}
        create(old)
        k("-n", tenant, "wait", "--for=jsonpath={.status.phase}=Bound", "pvc/sync-data", "--timeout=60s")
        old_uid = get("-n",tenant,"get","pvc","sync-data")["metadata"]["uid"]
        assert k("auth","can-i","patch","pv","--as="+ACTOR,check=False).stdout.strip() == b"no"
        assert k("auth","can-i","create","pvc","-n",tenant,"--as="+ACTOR,check=False).stdout.strip() == b"no"
        assert k("auth","can-i","create","pod","-n",a.NAMESPACE,"--as="+ACTOR,check=False).stdout.strip() == b"no"
        reports()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            keys = {}
            for role in ("witness","sync","history"):
                priv,pub = root/(role+".key"),root/(role+".pub")
                run("openssl","genpkey","-algorithm","ED25519","-out",str(priv))
                run("openssl","pkey","-in",str(priv),"-pubout","-out",str(pub))
                keys[role]=(priv,pub)
            config = root/"config"
            config.write_text(json.dumps({"tenants": {tenant: {role: {"cloud": role,
                "publicKey": str(keys[role][1]), "sourceUrl":"https://"+role+".example.invalid/git"}
                for role in ("sync","history")}}}))
            witness = w.Witness(root/"state",w.config(config,keys["witness"][0]),keys["witness"][0])
            witness.initialize(tenant)
            writer_fence.enroll(witness,tenant,{"sync":{"podUid":uid(),"nodeUid":ns[0]["uid"],"pvcUids":[old_uid]},
                                                "history":{"podUid":uid(),"nodeUid":ns[2]["uid"],"pvcUids":[uid()]}})
            anchor = witness.challenge(tenant,"anchor")
            def scan(role):
                return sign({"schema":1,"kind":"replica_scan","tenant":tenant,"role":role,"cloud":role,
                             "challengeDigest":digest(anchor["payload"]),"refs":{r:"1"*40 for r in w.REQUIRED},
                             "verifiedUnix":witness.now(),"appendOnlyVerified":True},keys[role][0])
            witness.anchor(tenant,scan("sync"),scan("history"))
            class Client:
                def call(self, request):
                    return witness_api.dispatch(witness,request)
            kc = json.loads(k("config","view","--raw","--minify","-o","json").stdout)["clusters"][0]["cluster"]
            ca,token = root/"ca",root/"token"
            ca.write_bytes(base64.b64decode(kc["certificate-authority-data"]))
            token.write_bytes(k("-n","gtm-system","create","token","gtm-repair-allocator","--duration=10m").stdout)
            api = Kube(kc["server"],str(ca),str(token))
            def reconcile(apply=True):
                return a.reconcile(api,Client(),keys["witness"][1],cfg,CLASS,tenant,"ci-incident","sync",apply=apply)
            assert reconcile(False)["state"] == "freeze_required"
            assert reconcile()["state"] == "waiting_for_capacity"
            f = witness.status(tenant)["writerFence"]
            assert f["phase"] == "freezing" and f["freeze"]["plan"] is None
            ch = f["freeze"]["challenge"]
            for name,i,cap in [(names[1],1,"9Gi"),(names[2],1,"12Gi"),(names[3],1,"20Gi"),(names[4],2,"10Gi")]:
                create(pv(name,i,cap))
            k("wait","--for=jsonpath={.status.phase}=Available","pv/"+names[2],"--timeout=60s")
            assert reconcile(False)["candidatePV"] == names[2]
            # No ordinary claim in tenant can consume the reserved class.
            misuse = json.loads(json.dumps(old))
            misuse["metadata"]["name"]="misuse"
            misuse["spec"].update(storageClassName=CLASS,volumeName=names[2])
            deny(["create","--dry-run=server","-f","-"],"GTMD static repair pool",json.dumps(misuse).encode())
            pod={"apiVersion":"v1","kind":"Pod","metadata":{"name":"probe","namespace":a.NAMESPACE},
                 "spec":{"containers":[{"name":"probe","image":"invalid.example/probe"}]}}
            deny(["create","--dry-run=server","-f","-"],"GTMD recovery namespace is host-staging only",json.dumps(pod).encode())
            try:
                initial=reconcile()
            except Exception:
                # Disposable CI: inspect admission type checking without printing credentials.
                print(k("get","validatingadmissionpolicy",*POLICIES,"-o","yaml").stdout.decode())
                raise
            assert initial["state"] in ("waiting_for_binding","allocated")
            name="repair-"+digest(ch)[:32]
            k("-n",a.NAMESPACE,"wait","--for=jsonpath={.status.phase}=Bound","pvc/"+name,"--timeout=60s")
            result=reconcile()
            assert result["state"]=="allocated" and result["targetPV"]==names[2]
            assert result==reconcile()
            f=witness.status(tenant)["writerFence"]
            assert f["freeze"]["challenge"]==ch and f["freeze"]["plan"]["targetPvcUid"]==result["targetPvcUid"]
            deny(["-n",a.NAMESPACE,"annotate","pvc",name,"gtmd.io/tenant=changed","--overwrite","--dry-run=server"],
                 "GTMD recovery claim binding and incident are immutable")
            deny(["-n",a.NAMESPACE,"delete","pvc",name,"--dry-run=server"],"GTMD recovery claims cannot be deleted")
            assert get("-n",tenant,"get","pvc","sync-data")["metadata"]["uid"]==old_uid
            assert get("get","pv",names[3])["status"]["phase"]=="Available"
            print("PASS: freeze without capacity, native best-fit static binding, one immutable incident target, no pod/ordinary claim escape.")
    finally:
        k("delete","validatingadmissionpolicybinding",*POLICIES,"--ignore-not-found",check=False)
        k("delete","validatingadmissionpolicy",*POLICIES,"--ignore-not-found",check=False)
        k("delete","ns",tenant,a.NAMESPACE,"--ignore-not-found","--wait=false",check=False)
        k("delete","pv",*names,"--ignore-not-found","--wait=false",check=False)
        k("delete","storageclass",NORMAL,CLASS,"--ignore-not-found",check=False)


if __name__ == "__main__":
    main()
