using System.Text.Json;
using GettingThingsMarkdown.ControlPlane;

namespace GettingThingsMarkdown.Tests;

public class TenantManifestTests
{
    [Fact]
    public void TenantGetsOnlyTwoPersistentPodsAndIndependentVolumes()
    {
        var id = Guid.Parse("f718c6aa-8be9-44ac-b8e7-0289bc058cdc");
        var secret = TenantManifests.NewSecrets();
        var manifests = TenantManifests.Build(id, "plus", 2, secret,
            "gtm/sync:test", "gtm/history:test").ToList();
        var workloads = manifests.Where(m => m.Kind == "StatefulSet").ToArray();
        var pvc = manifests.Where(m => m.Kind == "PersistentVolumeClaim").ToArray();
        Assert.Equal(2, workloads.Length);
        Assert.Equal(2, pvc.Length);
        Assert.Equal(new[] { "history", "sync" }, workloads.Select(m => m.Name).OrderBy(x => x));
        foreach (var set in workloads)
        {
            using var json = JsonDocument.Parse(set.Json);
            var spec = json.RootElement.GetProperty("spec");
            Assert.Equal(1, spec.GetProperty("replicas").GetInt32());
            Assert.Equal(1, spec.GetProperty("template").GetProperty("spec")
                .GetProperty("containers").GetArrayLength());
            var anti = spec.GetProperty("template").GetProperty("spec")
                .GetProperty("affinity").GetProperty("podAntiAffinity")
                .GetProperty("requiredDuringSchedulingIgnoredDuringExecution");
            Assert.Equal(1, anti.GetArrayLength());
            Assert.Equal("kubernetes.io/hostname",
                anti[0].GetProperty("topologyKey").GetString());
            Assert.Contains("2Gi", string.Join(" ", pvc.Select(p => p.Json)));
        }
        Assert.DoesNotContain("encryptionPassword", string.Join("", manifests.Select(m => m.Json)),
            StringComparison.OrdinalIgnoreCase);
        Assert.NotEqual(secret["admin-pass"], secret["sync-pass"]);
    }

    [Fact]
    public void LocalPVCIsIndependentOfSubscriptionQuota()
    {
        var id = Guid.NewGuid();
        var local = TenantManifests.Build(id, "pro", 2, null, "sync", "history")
            .First(x => x.Kind == "PersistentVolumeClaim");
        var production = TenantManifests.Build(id, "pro", 0, null, "sync", "history")
            .First(x => x.Kind == "PersistentVolumeClaim");
        Assert.Contains("2Gi", local.Json);
        Assert.Contains("500Gi", production.Json);
    }

    [Fact]
    public void LfsChangesSecondPvcToSmallMetadataVolume()
    {
        var id = Guid.NewGuid();
        var pvcs = TenantManifests.Build(id, "plus", 0, null, "sync", "history", true)
            .Where(x => x.Kind == "PersistentVolumeClaim").ToDictionary(x => x.Name);
        Assert.Contains("75Gi", pvcs["sync-data"].Json);
        Assert.Contains("8Gi", pvcs["history-data"].Json);
        var noLfs = TenantManifests.Build(id, "plus", 0, null, "sync", "history")
            .Single(x => x.Kind == "PersistentVolumeClaim" && x.Name == "history-data");
        Assert.Contains("75Gi", noLfs.Json);
    }

    [Fact]
    public void ProductionPVCStorageClassesCanBeSelectedIndependently()
    {
        var id = Guid.NewGuid();
        var pvcs = TenantManifests.Build(id, "plus", 0, null, "sync", "history",
            true, true, "gtm-hdd-quota", "hcloud-fast-small")
            .Where(x => x.Kind == "PersistentVolumeClaim").ToDictionary(x => x.Name);
        Assert.Contains("gtm-hdd-quota", pvcs["sync-data"].Json);
        Assert.DoesNotContain("gtm-hdd-quota", pvcs["history-data"].Json);
        Assert.Contains("hcloud-fast-small", pvcs["history-data"].Json);
    }

    [Fact]
    public void WormBackupRequiresDistinctTenantCredentials()
    {
        var id = Guid.NewGuid();
        var sets = TenantManifests.Build(id, "plus", 0, null, "sync", "history", true, true)
            .Where(x => x.Kind == "StatefulSet").ToDictionary(x => x.Name, x => x.Json);
        Assert.Contains("gtm-worm-credentials", sets["history"]);
        Assert.Contains("GTM_WORM_BACKUP_ENABLED", sets["history"]);
        Assert.DoesNotContain("gtm-worm-credentials", sets["sync"]);
    }

    [Fact]
    public void TenantNamesAreDnsSafeAndStable()
    {
        var id = Guid.Parse("5a535d7d-f0aa-433a-b98d-19351446d27f");
        Assert.Equal("gtm-5a535d7df0aa433ab98d19351446d27f", TenantNames.Namespace(id));
    }

    [Fact]
    public void HistoryPrivateKeyIsNeverMountedInSync()
    {
        var id = Guid.NewGuid();
        using var syncthing = JsonDocument.Parse(TenantManifests
            .Build(id, "basic", 2, null, "sync", "history")
            .Single(x => x.Kind == "StatefulSet" && x.Name == "sync").Json);
        string serialized = syncthing.RootElement.ToString();
        Assert.Contains("history-ca", serialized);
        Assert.DoesNotContain("history-tls", serialized);
        var pair = TenantCertificate.CreatePair(TenantNames.Namespace(id));
        using var publicSecret = JsonDocument.Parse(pair.Public.Json);
        Assert.True(publicSecret.RootElement.GetProperty("data").TryGetProperty("tls.crt", out _));
        Assert.False(publicSecret.RootElement.GetProperty("data").TryGetProperty("tls.key", out _));
    }

    [Fact]
    public void PreferDifferentAvailabilityZone()
    {
        var id = Guid.NewGuid();
        using var syncthing = JsonDocument.Parse(TenantManifests
            .Build(id, "basic", 2, null, "sync", "history")
            .Single(x => x.Kind == "StatefulSet" && x.Name == "sync").Json);
        var pod = syncthing.RootElement.GetProperty("spec").GetProperty("template")
            .GetProperty("spec");
        var preferred = pod.GetProperty("affinity").GetProperty("podAntiAffinity")
            .GetProperty("preferredDuringSchedulingIgnoredDuringExecution");
        Assert.Equal("topology.kubernetes.io/zone", preferred[0]
            .GetProperty("podAffinityTerm").GetProperty("topologyKey").GetString());
        Assert.Equal("ScheduleAnyway", pod.GetProperty("topologySpreadConstraints")[0]
            .GetProperty("whenUnsatisfiable").GetString());
    }

    [Theory]
    [InlineData("basic", "25m", "128Mi")]
    [InlineData("plus", "25m", "192Mi")]
    [InlineData("pro", "100m", "256Mi")]
    public void PerPlanResourcesHaveLowIdleRequestsButSafeBurstLimits(
        string plan, string cpu, string memory)
    {
        var id = Guid.NewGuid();
        var set = TenantManifests.Build(id, plan, 2, null, "sync", "history")
            .Single(x => x.Kind == "StatefulSet" && x.Name == "sync");
        using var json = JsonDocument.Parse(set.Json);
        var resources = json.RootElement.GetProperty("spec").GetProperty("template")
            .GetProperty("spec").GetProperty("containers")[0].GetProperty("resources");
        Assert.Equal(cpu, resources.GetProperty("requests").GetProperty("cpu").GetString());
        Assert.Equal(memory, resources.GetProperty("requests").GetProperty("memory").GetString());
        Assert.Equal("1", resources.GetProperty("limits").GetProperty("cpu").GetString());
    }

    [Fact]
    public void S3OffloadIsOptionalAndDoesNotExposeKeysToSync()
    {
        var id = Guid.NewGuid();
        var sets = TenantManifests.Build(id, "plus", 0, null, "sync", "history", true)
            .Where(x => x.Kind == "StatefulSet").ToDictionary(x => x.Name, x => x.Json);
        Assert.Contains("gtm-lfs-credentials", sets["history"]);
        Assert.Contains("MINIO_ENDPOINT", sets["history"]);
        Assert.Contains("LFS_START_SERVER", sets["history"]);
        Assert.Contains($"tenants/{TenantNames.Namespace(id)}/lfs/", sets["history"]);
        Assert.DoesNotContain("gtm-lfs-credentials", sets["sync"]);
        Assert.Contains("prepare-volume", sets["sync"]);
        Assert.DoesNotContain("prepare-volume", sets["history"]);
        Assert.DoesNotContain("MINIO_ACCESS_KEY_ID", sets["sync"]);
        var local = TenantManifests.Build(id, "plus", 0, null, "sync", "history", false)
            .Single(x => x.Kind == "StatefulSet" && x.Name == "history").Json;
        Assert.DoesNotContain("gtm-lfs-credentials", local);
    }

    [Fact]
    public void ServerCertificateMatchesInternalHistoryHost()
    {
        var resource = TenantCertificate.Create("gtm-abc");
        using var json = JsonDocument.Parse(resource.Json);
        Assert.Equal("kubernetes.io/tls", json.RootElement.GetProperty("type").GetString());
        Assert.True(json.RootElement.GetProperty("data").TryGetProperty("tls.crt", out _));
        Assert.True(json.RootElement.GetProperty("data").TryGetProperty("tls.key", out _));
    }
}
