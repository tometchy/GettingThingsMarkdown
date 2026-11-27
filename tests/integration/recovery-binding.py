import copy
import importlib.util
from pathlib import Path
import sys
import unittest

root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/"server/storage-controller"))
import recovery_binding as b
import controller as c
spec=importlib.util.spec_from_file_location("fixtures",root/"tests/integration/storage-controller.py")
fx=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fx)


class API:
    def __init__(self):
        self.nodes=[fx.live("n1"),fx.live("n2"),fx.live("n3")]
        self.reports=[fx.report("n1",fx.D1,"suspect"),fx.report("n2",fx.D2),fx.report("n3",fx.D3)]
        self.pvs=[]
        self.claims=[]
        self.pods=[]
        for n,d,claim in [("n1",fx.D1,"sync-data"),("n1",fx.D1,"sync-live"),
                          ("n3",fx.D3,"history-data"),("n2",fx.D2,"replacement")]:
            pv=fx.pv(n,d,"pv-"+claim,True)
            pv["spec"]["capacity"]={"storage":"10Gi"}
            pv["spec"]["accessModes"]=["ReadWriteOnce"]
            pv["spec"]["claimRef"]={"namespace":fx.TENANT,"name":claim,"uid":claim+"-uid"}
            self.pvs.append(pv)
            self.claims.append({"metadata":{"name":claim,"uid":claim+"-uid","namespace":fx.TENANT},
                "spec":{"volumeName":"pv-"+claim},"status":{"phase":"Bound"}})
        for role,node,claims in [("sync","n1",["sync-data","sync-live"]),("history","n3",["history-data"])]:
            self.pods.append({"metadata":{"name":role+"-0","namespace":fx.TENANT,"uid":role+"-pod"},
                "spec":{"nodeName":node,"volumes":[{"persistentVolumeClaim":{"claimName":x}} for x in claims]}})

    def list(self,path):
        ns="/api/v1/namespaces/"+fx.TENANT
        return iter({ns+"/pods":self.pods,ns+"/persistentvolumeclaims":self.claims,
                     "/api/v1/persistentvolumes":self.pvs,"/api/v1/nodes":self.nodes,
                     "/api/v1/namespaces/gtm-storage-reports/configmaps":self.reports}[path])


class Tests(unittest.TestCase):
    def setUp(self):
        self.api=API()
        self.cfg=c.inventory(fx.RAW)

    def inspect(self):
        return b.inspect(self.api,self.cfg,fx.TENANT,"sync","sync-data","replacement",fx.NOW)

    def test_all_writer_pvcs_and_healthy_candidate(self):
        v=self.inspect()
        self.assertEqual(v["writers"]["sync"]["pvcUids"],["sync-data-uid","sync-live-uid"])
        self.assertEqual(v["targetPvcUid"],"replacement-uid")
        self.assertFalse(v["activationAllowed"])
        self.assertFalse(v["physicalMountVerified"])

    def test_wrong_binding_or_pod_reference(self):
        self.api.pvs[3]["spec"]["claimRef"]["uid"]="different"
        with self.assertRaises(ValueError):
            self.inspect()
        self.api.pvs[3]["spec"]["claimRef"]["uid"]="replacement-uid"
        self.api.pods[0]["spec"]["volumes"].append({"persistentVolumeClaim":{"claimName":"replacement"}})
        with self.assertRaises(ValueError):
            self.inspect()

    def test_stale_health_or_capacity(self):
        self.api.reports[1]=fx.report("n2",fx.D2,ts=fx.NOW-301)
        with self.assertRaises(ValueError):
            self.inspect()
        self.api.reports[1]=fx.report("n2",fx.D2)
        self.api.pvs[3]["spec"]["capacity"]["storage"]="1Gi"
        with self.assertRaises(ValueError):
            self.inspect()

    def test_shared_pid_namespace_rejected(self):
        self.api.pods[0]["spec"]["shareProcessNamespace"]=True
        with self.assertRaises(ValueError):
            self.inspect()


if __name__=="__main__":
    unittest.main()
