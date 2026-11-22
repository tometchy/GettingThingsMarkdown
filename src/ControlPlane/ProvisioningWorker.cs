using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using Microsoft.EntityFrameworkCore;

namespace GettingThingsMarkdown.ControlPlane;

public sealed class ProvisioningWorker(
    IServiceScopeFactory scopes, IConfiguration config,
    ILogger<ProvisioningWorker> logger) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        if (!config.GetValue<bool>("GTM_KUBE_ENABLED")) return;
        if (!KubeApi.Available)
        {
            logger.LogError("GTM_KUBE_ENABLED requires in-cluster Kubernetes service-account credentials.");
            return;
        }
        while (!stoppingToken.IsCancellationRequested)
        {
            try { await ReconcileOnceAsync(stoppingToken); }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
            catch (Exception ex) { logger.LogError(ex, "Tenant reconciler failed. Retrying."); }
            await Task.Delay(TimeSpan.FromSeconds(20), stoppingToken);
        }
    }

    private async Task ReconcileOnceAsync(CancellationToken ct)
    {
        using var scope = scopes.CreateScope();
        var accounts = scope.ServiceProvider.GetRequiredService<AppDb>();
        var stateDb = scope.ServiceProvider.GetRequiredService<ProvisioningDb>();
        using var kube = new KubeApi();
        foreach (var account in await accounts.Accounts.AsNoTracking().ToListAsync(ct))
        {
            var state = await stateDb.Tenants.FindAsync([account.Id], ct);
            if (state is null)
            {
                state = new TenantState
                {
                    AccountId = account.Id, Namespace = TenantNames.Namespace(account.Id),
                    Status = "Pending", UpdatedUtc = DateTimeOffset.UtcNow
                };
                stateDb.Tenants.Add(state);
                await stateDb.SaveChangesAsync(ct);
            }
            try
            {
                // The trial state only determines eligibility to provision. Data is never
                // deleted on expiration. Storage enforcement is an independent future gate.
                if (!PlanCatalog.View(account, DateTimeOffset.UtcNow).CanWrite)
                {
                    if (state.Status == "Pending") state.Status = "Expired";
                    continue;
                }
                if (state.Status == "Ready") continue;
                state.Status = "Provisioning";
                await EnsureTenantAsync(kube, state.Namespace, account, ct);
                bool ready = await kube.StatefulSetReadyAsync(state.Namespace, "sync", ct) &&
                             await kube.StatefulSetReadyAsync(state.Namespace, "history", ct);
                if (ready)
                {
                    var secret = await kube.SecretAsync(state.Namespace, "gtm-credentials", ct);
                    if (secret is null) throw new InvalidOperationException("Missing tenant credentials.");
                    var sync = new SyncthingTenantApi(state.Namespace, secret["sync-api-key"]);
                    var serverId = await sync.DeviceIdAsync(ct);
                    if (await GiteaTenantApi.RepositoryReadyAsync(state.Namespace,
                        await kube.SecretAsync(state.Namespace, "history-tls", ct), ct))
                    {
                        state.ServerDeviceId = serverId;
                        state.Status = "Ready";
                    }
                }
                state.LastError = null;
            }
            catch (Exception ex)
            {
                logger.LogWarning(ex, "Provisioning failed for tenant {Namespace}", state.Namespace);
                state.Status = "Failed";
                state.LastError = ex.GetType().Name + ": " + ex.Message[..Math.Min(ex.Message.Length, 240)];
            }
            finally
            {
                state.UpdatedUtc = DateTimeOffset.UtcNow;
                await stateDb.SaveChangesAsync(ct);
            }
        }
    }

    private async Task EnsureTenantAsync(KubeApi kube, string ns, Account account,
        CancellationToken ct)
    {
        // Create the namespace first; later objects live inside it.
        var declarations = TenantManifests.Build(account.Id, account.Plan,
            config.GetValue<int>("GTM_DEV_VOLUME_GI"), null,
            config["GTM_SYNC_IMAGE"] ?? "gtm/sync:dev",
            config["GTM_HISTORY_IMAGE"] ?? "gtm/history:dev").ToList();
        await kube.ApplyAsync(declarations[0], ct);

        if (!await kube.ExistsAsync($"/api/v1/namespaces/{ns}/secrets/gtm-credentials", ct))
        {
            var initial = TenantManifests.Build(account.Id, account.Plan,
                config.GetValue<int>("GTM_DEV_VOLUME_GI"),
                TenantManifests.NewSecrets(), config["GTM_SYNC_IMAGE"] ?? "gtm/sync:dev",
                config["GTM_HISTORY_IMAGE"] ?? "gtm/history:dev");
            await kube.ApplyAsync(initial.Single(x => x.Kind == "Secret"), ct);
        }
        if (!await kube.ExistsAsync($"/api/v1/namespaces/{ns}/secrets/history-tls", ct))
        {
            var pair = TenantCertificate.CreatePair(ns);
            await kube.ApplyAsync(pair.Server, ct);
            await kube.ApplyAsync(pair.Public, ct);
        }
        else if (!await kube.ExistsAsync($"/api/v1/namespaces/{ns}/secrets/history-ca", ct))
        {
            var existing = await kube.SecretAsync(ns, "history-tls", ct)
                ?? throw new InvalidOperationException("Missing Gitea certificate.");
            await kube.ApplyAsync(TenantCertificate.PublicOnly(ns, existing["tls.crt"]), ct);
        }

        // Apply Services before StatefulSets. A/B must remain in different nodes.
        foreach (var resource in declarations.Skip(1))
            await kube.ApplyAsync(resource, ct);
    }
}

public static class TenantCertificate
{
    public static (KubeObject Server, KubeObject Public) CreatePair(string ns)
    {
        var server = Create(ns);
        using var json = JsonDocument.Parse(server.Json);
        var base64 = json.RootElement.GetProperty("data").GetProperty("tls.crt").GetString()!;
        var pem = Encoding.UTF8.GetString(Convert.FromBase64String(base64));
        return (server, PublicOnly(ns, pem));
    }

    public static KubeObject PublicOnly(string ns, string pem) =>
        KubeObject.From("v1", "Secret", "history-ca", ns, new
        {
            apiVersion = "v1", kind = "Secret",
            metadata = new { name = "history-ca", @namespace = ns },
            type = "Opaque",
            data = new Dictionary<string,string>
            {
                ["tls.crt"] = Convert.ToBase64String(Encoding.UTF8.GetBytes(pem))
            }
        });

    public static KubeObject Create(string ns)
    {
        string dns = $"history.{ns}.svc.cluster.local";
        using var rsa = RSA.Create(2048);
        var request = new CertificateRequest($"CN={dns}", rsa,
            HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
        var san = new SubjectAlternativeNameBuilder();
        san.AddDnsName(dns);
        san.AddDnsName($"history.{ns}.svc");
        request.CertificateExtensions.Add(san.Build());
        request.CertificateExtensions.Add(new X509KeyUsageExtension(
            X509KeyUsageFlags.DigitalSignature | X509KeyUsageFlags.KeyEncipherment, true));
        request.CertificateExtensions.Add(new X509EnhancedKeyUsageExtension(
            new OidCollection { new("1.3.6.1.5.5.7.3.1") }, false));
        using var cert = request.CreateSelfSigned(DateTimeOffset.UtcNow.AddMinutes(-5),
            DateTimeOffset.UtcNow.AddYears(1));
        var data = new Dictionary<string, string>
        {
            ["tls.crt"] = Convert.ToBase64String(Encoding.UTF8.GetBytes(cert.ExportCertificatePem())),
            ["tls.key"] = Convert.ToBase64String(Encoding.UTF8.GetBytes(rsa.ExportPkcs8PrivateKeyPem()))
        };
        return KubeObject.From("v1", "Secret", "history-tls", ns, new
        {
            apiVersion = "v1", kind = "Secret",
            metadata = new { name = "history-tls", @namespace = ns },
            type = "kubernetes.io/tls", data
        });
    }
}
