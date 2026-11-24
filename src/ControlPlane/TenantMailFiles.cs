using System.Net.Http.Json;
using System.Text.Json;

namespace GettingThingsMarkdown.ControlPlane;

/// <summary>Append encrypted envelopes to both tenant Git replicas before SMTP ACK.
/// No shell access, Git writer password or private key is exposed to operators.</summary>
public static class TenantMailFiles
{
    public static async Task PersistSettingsAsync(MailInbox inbox, CancellationToken ct)
    {
        string ns = TenantNames.Namespace(inbox.AccountId);
        using var kube = new KubeApi();
        var secrets = await kube.SecretAsync(ns, "gtm-credentials", ct)
            ?? throw new InvalidOperationException("Tenant storage is not available.");
        using var http = new HttpClient { BaseAddress = new Uri($"http://sync.{ns}.svc.cluster.local:8385/"),
            Timeout = TimeSpan.FromSeconds(180) };
        http.DefaultRequestHeaders.Add("X-API-Key", secrets["sync-api-key"]);
        using var result = await http.PostAsJsonAsync("settings", new {
            accountId = inbox.AccountId, localPart = inbox.LocalPart,
            publicKeys = MailKeySet.Read(inbox.PublicKeyBase64), enabled = inbox.Enabled,
            createdUtc = inbox.CreatedUtc }, ct);
        result.EnsureSuccessStatusCode();
    }

    public static async Task<MailEnvelope> FetchAsync(Guid accountId, string digest,
        CancellationToken ct)
    {
        string ns = TenantNames.Namespace(accountId);
        using var kube = new KubeApi();
        var secrets = await kube.SecretAsync(ns, "gtm-credentials", ct)
            ?? throw new InvalidOperationException("Tenant storage is not available.");
        using var http = new HttpClient { BaseAddress = new Uri($"http://sync.{ns}.svc.cluster.local:8385/"),
            Timeout = TimeSpan.FromSeconds(180) };
        http.DefaultRequestHeaders.Add("X-API-Key", secrets["sync-api-key"]);
        return await http.GetFromJsonAsync<MailEnvelope>("mail/" + digest, ct)
            ?? throw new InvalidOperationException("Missing durable Git envelope.");
    }
    public static async Task<MailEnvelope> PersistAsync(Guid accountId, string digest,
        MailEnvelope envelope, CancellationToken ct)
    {
        string ns = TenantNames.Namespace(accountId);
        using var kube = new KubeApi();
        var secrets = await kube.SecretAsync(ns, "gtm-credentials", ct)
            ?? throw new InvalidOperationException("Tenant storage is not available.");
        using var http = new HttpClient { BaseAddress = new Uri($"http://sync.{ns}.svc.cluster.local:8385/"),
            Timeout = TimeSpan.FromSeconds(180) };
        http.DefaultRequestHeaders.Add("X-API-Key", secrets["sync-api-key"]);
        using var result = await http.PostAsJsonAsync("mail/" + digest, envelope, ct);
        result.EnsureSuccessStatusCode();
        return await result.Content.ReadFromJsonAsync<MailEnvelope>(cancellationToken: ct)
            ?? throw new InvalidOperationException("Missing durable Git envelope receipt.");
    }
}
