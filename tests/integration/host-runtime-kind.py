#!/usr/bin/env python3
"""Real CRI smoke test on a disposable kind cluster (no tenant data)."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server/integrity"))
from host_runtime_fence import runtime_view, mount_rows
from recovery_crypto import RecoveryError

TENANT = "gtm-" + uuid.uuid4().hex


def run(*args, input=None):
    return subprocess.check_output(args, input=input, timeout=90)


def k(*args, input=None):
    return run("kubectl", *args, input=input)


def get(*args):
    return json.loads(k(*args, "-o", "json"))


def wait_for_view(cri, cfg):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            result = runtime_view(*cri(), cfg)
            if result["exact"] and result["nonExitedContainerCount"] > 0:
                return result
        except RecoveryError:
            pass
        time.sleep(0.5)
    raise RuntimeError("real CRI writer not observed")


def main():
    k("create", "namespace", TENANT)
    try:
        # Use the same pause image already in the node's local image store.
        # No separate workload image or privileged container is required.
        nodes = run("kind", "get", "nodes", "--name", "gtm").decode().splitlines()
        node = nodes[0]
        images = json.loads(run("docker", "exec", node, "crictl", "images", "-o", "json"))
        tags = [t for image in images["images"] for t in image.get("repoTags", [])
                if "/pause:" in t]
        if not tags:
            raise RuntimeError("kind pause image missing")
        pod = {"apiVersion": "v1", "kind": "Pod",
               "metadata": {"name": "sync-0", "namespace": TENANT},
               "spec": {"nodeName": node, "restartPolicy": "Never",
                        "containers": [{"name": "writer-fixture", "image": tags[0],
                                        "imagePullPolicy": "Never"}]}}
        k("apply", "-f", "-", input=json.dumps(pod).encode())
        k("-n", TENANT, "wait", "--for=condition=Ready", "pod/sync-0", "--timeout=60s")
        pod_uid = get("-n", TENANT, "get", "pod", "sync-0")["metadata"]["uid"]
        node_uid = get("get", "node", node)["metadata"]["uid"]
        cfg = {"tenant": TENANT, "role": "sync",
               "identity": {"podUid": pod_uid, "nodeUid": node_uid, "pvcUids": [str(uuid.uuid4())]}}

        def cri():
            pods = json.loads(run("docker", "exec", node, "crictl", "pods", "-o", "json"))
            containers = json.loads(run("docker", "exec", node, "crictl", "ps", "-a", "-o", "json"))
            return pods["items"], containers["containers"]

        before = wait_for_view(cri, cfg)
        assert before["replacementCount"] == 0
        assert before["activeSandboxCount"] > 0
        assert before["nonExitedContainerCount"] > 0
        # Parse the real node mount namespace, not fabricated mount data.
        mountinfo = run("docker", "exec", node, "cat", "/proc/1/mountinfo").decode()
        dev = mountinfo.splitlines()[0].split()[2]
        assert mount_rows(mountinfo, {dev})[0] >= 1
        # Exercise actual CRI stop only for this disposable enrolled sandbox.
        stopped = before["exact"]
        for sandbox in stopped:
            run("docker", "exec", node, "crictl", "--timeout", "15s", "stopp", sandbox)
        # Kubelet may recreate a sandbox; never interpret this as fencing.
        pods, containers = cri()
        for p in pods:
            if p["id"] in stopped:
                assert p["state"] == "SANDBOX_NOTREADY"
        for c in containers:
            if c["podSandboxId"] in stopped:
                assert c["state"] == "CONTAINER_EXITED"
        print("Real CRI observed and stopped disposable writer sandbox; no durable fence asserted.")
    finally:
        k("delete", "namespace", TENANT, "--wait=false")


if __name__ == "__main__":
    main()
