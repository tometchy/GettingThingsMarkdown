#!/usr/bin/env python3
"""Revalidate an allocated target against the actual host mount before staging.

Reuse the existing allocator in read-only mode, LVM identity checks and
mountinfo parser. No new service, storage allocation, mount action or key.
"""
from pathlib import Path

from controller import mapping, metadata, name
from recovery_crypto import RecoveryError
from recovery_witness import require, TENANT, identifier, ROLES
from host_runtime_fence import UID
from local_lvm_fence import MACHINE, VG, LVM_UUID
from lvm_mount_binding import verify_mount
from repair_allocator import reconcile as inspect_allocation


def configuration(value):
    require(isinstance(value, dict) and set(value) == {
        "tenant", "role", "incident", "machineId", "nodeName", "pvName", "pvUid",
        "pvcUid", "mount", "vgName", "vgUuid", "lvUuid"}, "invalid_stage_binding_inventory")
    require(isinstance(value["tenant"], str) and TENANT.fullmatch(value["tenant"])
            and value["role"] in ROLES and identifier(value["incident"])
            and isinstance(value["machineId"], str) and MACHINE.fullmatch(value["machineId"])
            and name(value["nodeName"]) and name(value["pvName"])
            and all(isinstance(value[k], str) and UID.fullmatch(value[k]) for k in ("pvUid", "pvcUid"))
            and isinstance(value["vgName"], str) and VG.fullmatch(value["vgName"])
            and all(isinstance(value[k], str) and LVM_UUID.fullmatch(value[k]) for k in ("vgUuid", "lvUuid"))
            and isinstance(value["mount"], str) and Path(value["mount"]).is_absolute()
            and Path(value["mount"]) != Path("/"), "invalid_stage_binding_inventory")
    return value


def verify_binding(kube, witness, public, storage_inventory, repair_class, host_inventory,
                   identity, operation, lvm, machine_id, mountinfo="/proc/self/mountinfo"):
    cfg = configuration(host_inventory)
    require(machine_id == cfg["machineId"] and cfg["tenant"] == identity["tenant"]
            and cfg["role"] == identity["target_role"] and cfg["pvcUid"] == identity["target_pvc_uid"],
            "staging_host_or_target_identity_mismatch")
    # Read-only: check native binding, old enrolled PVC, current signed freeze,
    # exact incident, pinned PV UID, target health and failure domain.
    result = inspect_allocation(kube, witness, public, storage_inventory, repair_class,
                                cfg["tenant"], cfg["incident"], cfg["role"], apply=False)
    require(result["state"] == "allocated" and result["targetPvcUid"] == cfg["pvcUid"]
            and result["targetPV"] == cfg["pvName"] and result["activationAllowed"] is False,
            "staging_allocation_not_verified")
    status = witness.call({"tenant": cfg["tenant"], "action": "status"})
    repair = status.get("repair")
    require(status["generation"] == identity["generation"] and repair
            and repair["operation"] == operation and repair["targetPvcUid"] == cfg["pvcUid"]
            and repair["targetRole"] == cfg["role"]
            and repair["state"] in ("authorized", "ticket_issued", "staged_verified")
            and status["writerFence"]["phase"] == "frozen", "staging_repair_not_current")
    pv = kube.get("/api/v1/persistentvolumes/" + cfg["pvName"])
    where = mapping(pv, storage_inventory[1])
    node = storage_inventory[1].get(cfg["nodeName"])
    require(node and node["cloud"] == identity["target_cloud"] and where
            and where[0] == cfg["nodeName"] and metadata(pv).get("uid") == cfg["pvUid"]
            and metadata(pv).get("name") == cfg["pvName"]
            and not metadata(pv).get("deletionTimestamp")
            and pv["spec"]["local"]["path"] == cfg["mount"]
            and pv["spec"]["claimRef"]["uid"] == cfg["pvcUid"]
            and pv["status"]["phase"] == "Bound", "staging_pv_or_host_mapping_mismatch")
    # The trusted host has already provisioned/tagged the exact LV for this
    # PVC. Never derive a device from a mutable major:minor alone.
    lv_cfg = {"tenant": cfg["tenant"], "role": cfg["role"],
              "volumes": {cfg["pvcUid"]: {k: cfg[k] for k in ("vgName", "vgUuid", "lvUuid")}}}
    state = lvm.inspect(lv_cfg, cfg["pvcUid"])
    mounted = verify_mount(Path(cfg["mount"]), state, Path(mountinfo).read_text())
    return {"mount": cfg["mount"], **mounted}
