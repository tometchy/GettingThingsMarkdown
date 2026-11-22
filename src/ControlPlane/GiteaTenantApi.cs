using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
using System.Text;

namespace GettingThingsMarkdown.ControlPlane;

public static class GiteaTenantApi
{
    public static async Task<bool> RepositoryReadyAsync(string ns,
        Dictionary<string, string>? tls, CancellationToken ct)
    {
        if (tls is null || !tls.TryGetValue("tls.crt", out string? pem))
            return false;
        var pinned = X509Certificate2.CreateFromPem(pem);
        using var handler = new HttpClientHandler
        {
            ServerCertificateCustomValidationCallback = (_, cert, _, errors) =>
                cert is not null && (errors & SslPolicyErrors.RemoteCertificateNameMismatch) == 0 &&
                pinned.RawData.SequenceEqual(cert.RawData)
        };
        using var client = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(8) };
        try
        {
            using var r = await client.GetAsync(
                $"https://history.{ns}.svc.cluster.local:3000/api/v1/version", ct);
            return r.IsSuccessStatusCode;
        }
        catch (HttpRequestException) { return false; }
        catch (TaskCanceledException) when (!ct.IsCancellationRequested) { return false; }
    }
}
