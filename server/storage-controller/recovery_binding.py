#!/usr/bin/env python3
"""Read-only verification of the K8s identity needed by recovery witness.

An API binding is not physical fencing or proof that a mount is unused on
an unreachable node. No allocation, pod deletion, PVC mutation or activation.
"""
import argparse
import json
from pathlib import Path
import re
import time

from controller import inventory, mapping, report_states, metadata, ident, TENANT, CLAIMS
from kube_client import Kube, ApiError

SIZE = re.compile(r"([0-9]+)(Ki|Mi|Gi|Ti|Pi|K|M|G|T|P)?\Z")
MULT = {None:1, **{x:1024**i for i,x in enumerate(("Ki","Mi","Gi","Ti","Pi"),1)},
        **{x:1000**i for i,x in enumerate(("K","M","G","T","P"),1)}}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def size(value):
    require(isinstance(value,str) and SIZE.fullmatch(value),"unsupported_storage_quantity")
    number,unit=SIZE.fullmatch(value).groups()
    return int(number)*MULT[unit]


def bound(pvc,pv,tenant):
    m=metadata(pvc)
    ref=pv.get("spec",{}).get("claimRef") or {}
    require(m.get("namespace")==tenant and m.get("uid")
            and not m.get("deletionTimestamp")
            and pvc.get("status",{}).get("phase")=="Bound"
            and pvc.get("spec",{}).get("volumeName")==ident(pv)
            and ref.get("namespace")==tenant and ref.get("name")==ident(pvc)
            and ref.get("uid")==m["uid"] and pv.get("status",{}).get("phase")=="Bound"
            and not metadata(pv).get("deletionTimestamp")
            and pv.get("spec",{}).get("persistentVolumeReclaimPolicy")=="Retain",
            "claim_binding_not_verified")


def claims_in(pod):
    return [v["persistentVolumeClaim"]["claimName"] for v in pod.get("spec",{}).get("volumes",[])
            if "persistentVolumeClaim" in v]


def writer_identity(pod, tenant, role, pvcs, volumes, live_nodes, config):
    m,spec=metadata(pod),pod.get("spec",{})
    require(m.get("namespace")==tenant and ident(pod)==role+"-0" and m.get("uid")
            and not m.get("deletionTimestamp")
            and not spec.get("hostPID") and not spec.get("shareProcessNamespace"),
            "writer_pod_identity_or_namespace_invalid")
    node=live_nodes.get(spec.get("nodeName"))
    require(node and metadata(node).get("uid"),"writer_node_missing")
    node_name=ident(node)
    require(node_name in config[1] and config[1][node_name]["uid"]==metadata(node)["uid"],
            "writer_node_inventory_mismatch")
    names=claims_in(pod)
    permitted={"sync-data","sync-live"} if role=="sync" else {"history-data"}
    required="sync-data" if role=="sync" else "history-data"
    require(required in names and len(set(names))==len(names) and set(names)<=permitted,
            "unexpected_writer_claims")
    uids=[]
    for claim in names:
        pvc=pvcs.get(claim)
        require(pvc is not None,"writer_claim_missing")
        pv=volumes.get(pvc.get("spec",{}).get("volumeName"))
        require(pv is not None,"writer_volume_missing")
        bound(pvc,pv,tenant)
        where=mapping(pv,config[1])
        require(where and where[0]==node_name and pv["spec"]["storageClassName"] in config[0],
                "writer_volume_mapping_mismatch")
        uids.append(metadata(pvc)["uid"])
    return {"podUid":m["uid"],"nodeUid":metadata(node)["uid"],"pvcUids":sorted(uids)}


def inspect(api, config, tenant, target_role, old_claim, new_claim, now=None):
    now=time.time() if now is None else now
    require(TENANT.fullmatch(tenant) and target_role in ("sync","history")
            and CLAIMS.get(old_claim)==target_role and old_claim!=new_claim,
            "invalid_recovery_target")
    require(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?",new_claim),"invalid_target_claim")
    pods=list(api.list("/api/v1/namespaces/"+tenant+"/pods"))
    by_pod={ident(p):p for p in pods}
    pvcs={ident(p):p for p in api.list("/api/v1/namespaces/"+tenant+"/persistentvolumeclaims")}
    pvs={ident(p):p for p in api.list("/api/v1/persistentvolumes")}
    nodes={ident(n):n for n in api.list("/api/v1/nodes")}
    reports={ident(r):r for r in api.list("/api/v1/namespaces/gtm-storage-reports/configmaps")}
    identities={}
    for role in ("sync","history"):
        require(role+"-0" in by_pod,"writer_pod_missing")
        identities[role]=writer_identity(by_pod[role+"-0"],tenant,role,pvcs,pvs,nodes,config)
    require(identities["sync"]["nodeUid"]!=identities["history"]["nodeUid"],"writer_node_collision")
    old,new=pvcs.get(old_claim),pvcs.get(new_claim)
    require(old is not None and new is not None,"recovery_claim_missing")
    require(metadata(old).get("uid") in identities[target_role]["pvcUids"],"old_claim_not_mounted")
    require(metadata(new).get("uid") not in {u for v in identities.values() for u in v["pvcUids"]},
            "target_claim_already_in_use")
    require(not any(new_claim in claims_in(p) for p in pods),"target_claim_referenced_by_pod")
    old_pv,new_pv=(pvs.get(p.get("spec",{}).get("volumeName")) for p in (old,new))
    require(old_pv and new_pv,"recovery_volume_missing")
    bound(old,old_pv,tenant)
    bound(new,new_pv,tenant)
    old_where,new_where=mapping(old_pv,config[1]),mapping(new_pv,config[1])
    require(old_where and new_where and old_where[0]!=new_where[0]
            and config[1][old_where[0]]["cloud"]==config[1][new_where[0]]["cloud"],
            "target_not_on_distinct_same_cloud_node")
    other_role="history" if target_role=="sync" else "sync"
    other_node=by_pod[other_role+"-0"]["spec"]["nodeName"]
    require(config[1][old_where[0]]["cloud"]!=config[1][other_node]["cloud"],
            "replicas_not_in_distinct_clouds")
    node=config[1][new_where[0]]
    require(report_states(node,nodes.get(new_where[0]),reports.get(new_where[0]),now)[new_where[1]]=="healthy",
            "target_disk_not_verified_healthy")
    a,b=old_pv["spec"],new_pv["spec"]
    require(a.get("storageClassName")==b.get("storageClassName") and a.get("accessModes")==b.get("accessModes")
            and a.get("volumeMode","Filesystem")==b.get("volumeMode","Filesystem")=="Filesystem"
            and size(b.get("capacity",{}).get("storage"))>=size(a.get("capacity",{}).get("storage")),
            "target_storage_incompatible")
    return {"tenant":tenant,"writers":identities,"targetRole":target_role,
            "oldPvcUid":metadata(old)["uid"],"targetPvcUid":metadata(new)["uid"],
            "targetNodeUid":node["uid"],"activationAllowed":False,
            "physicalMountVerified":False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for k in ("inventory","tenant","target-role","old-claim","new-claim"):
        p.add_argument("--"+k,required=True)
    p.add_argument("--endpoint",default="https://kubernetes.default.svc")
    p.add_argument("--ca",default="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    p.add_argument("--token",default="/var/run/secrets/kubernetes.io/serviceaccount/token")
    a=p.parse_args()
    try:
        result=inspect(Kube(a.endpoint,a.ca,a.token),inventory(json.loads(Path(a.inventory).read_text())),
                       a.tenant,a.target_role,a.old_claim,a.new_claim)
        print(json.dumps(result,separators=(",",":")))
    except (OSError,ValueError,TypeError,KeyError,ApiError) as exc:
        print(json.dumps({"state":"blocked","reason":str(exc) if isinstance(exc,ValueError) else type(exc).__name__}))
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
