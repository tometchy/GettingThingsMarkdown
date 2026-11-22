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
        Assert.Contains("1024Gi", production.Json);
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
