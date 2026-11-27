"""Real etcd quorum/CAS test; disposable loopback cluster only."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"server/integrity"))
from witness_etcd import EtcdStore
from recovery_crypto import RecoveryError
from recovery_witness import Witness, config

TENANT="gtm-"+"e"*32
OTHER="gtm-"+"f"*32


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--endpoint",action="append",required=True)
    p.add_argument("--stop",action="append",required=True)
    a=p.parse_args()
    store=EtcdStore(a.endpoint,insecure_test=True,timeout=3)
    for _ in range(30):
        try:
            store.read(TENANT)
            break
        except RecoveryError:
            time.sleep(1)
    else:
        raise RuntimeError("etcd not ready")
    assert store.read(TENANT)==(None,0)
    assert store.cas(TENANT,0,b"first")
    raw,rev=store.read(TENANT)
    assert raw==b"first"
    assert not store.cas(TENANT,0,b"wrong")
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(lambda i:store.cas(TENANT,rev,str(i).encode()),range(5)))
    assert sum(results)==1
    assert not store.cas(TENANT,rev,b"stale")
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        keys={}
        for role in ("witness","sync","history"):
            key,pub=root/(role+".key"),root/(role+".pub")
            subprocess.run(["openssl","genpkey","-algorithm","ED25519","-out",str(key)],check=True)
            subprocess.run(["openssl","pkey","-in",str(key),"-pubout","-out",str(pub)],check=True)
            keys[role]=(key,pub)
        c=root/"config.json"
        c.write_text(json.dumps({"tenants":{OTHER:{role:{"cloud":role,"publicKey":str(keys[role][1]),
            "sourceUrl":"https://"+role+".example.invalid/repo.git"} for role in ("sync","history")}}}))
        cfg=config(c,keys["witness"][0])
        w1=Witness(None,cfg,keys["witness"][0],store=store)
        w2=Witness(None,cfg,keys["witness"][0],store=EtcdStore(a.endpoint,insecure_test=True,timeout=3))
        w1.initialize(OTHER)
        assert w2.status(OTHER)["generation"]==0
        def challenge(w):
            try:
                w.challenge(OTHER,"anchor")
                return True
            except RecoveryError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(challenge,[w1,w2]))==1
        assert w1.status(OTHER)["challenge"]==w2.status(OTHER)["challenge"]
        before=store.read(OTHER)
        subprocess.run(["docker","stop",*a.stop],check=True,stdout=subprocess.DEVNULL)
        try:
            single=EtcdStore([a.endpoint[0]],insecure_test=True,timeout=3)
            try:
                single.read(OTHER)
            except RecoveryError:
                pass
            else:
                raise AssertionError("minority accepted linearizable read")
            try:
                single.cas(OTHER,before[1],b"minority")
            except RecoveryError:
                pass
            else:
                raise AssertionError("minority accepted transaction")
        finally:
            subprocess.run(["docker","start",*a.stop],check=True,stdout=subprocess.DEVNULL)
        for _ in range(30):
            try:
                after=store.read(OTHER)
                break
            except RecoveryError:
                time.sleep(1)
        else:
            raise AssertionError("quorum did not return")
        assert after==before
        print("PASS: real three-member etcd CAS, concurrent witnesses, minority refuses reads/writes, recovery preserves signed state")


if __name__=="__main__":
    main()
