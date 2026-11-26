using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
using System.Text;

namespace GettingThingsMarkdown.ControlPlane;

public static class GiteaTenantApi
{
    public static async Task<bool> RepositoryReadyAsync(string ns,
        Dictionary<string, string>? tls, string archivePassword, CancellationToken ct)
    {
        if (tls is null || !tls.TryGetValue("tls.crt", out string? pem))
            return false;
        var pinned = X509CertificateLoader.LoadCertificate(Encoding.UTF8.GetBytes(pem));
        using var handler = new HttpClientHandler
        {
            ServerCertificateCustomValidationCallback = (_, cert, _, errors) =>
                cert is not null && (errors & SslPolicyErrors.RemoteCertificateNameMismatch) == 0 &&
                pinned.RawData.SequenceEqual(cert.RawData)
        };
        using var client = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(8) };
        if (string.IsNullOrWhiteSpace(archivePassword)) return false;
        client.DefaultRequestHeaders.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue(
            "Basic", Convert.ToBase64String(Encoding.UTF8.GetBytes("reader:" + archivePassword)));
        try
        {
            using var r = await client.GetAsync(
                $"https://history.{ns}.svc.cluster.local:3000/api/v1/repos/archive/history/branches/main", ct);
            // Repository creation alone is insufficient: Sync may not yet have
            // pushed the initial main commit. Do not expose a ready tenant
            // before the reader can resolve main in the second replica.
            return r.IsSuccessStatusCode;
        }
        catch (HttpRequestException) { return false; }
        catch (TaskCanceledException) when (!ct.IsCancellationRequested) { return false; }
    }
}
