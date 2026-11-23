using System.Net;
using System.Net.Http.Headers;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.RegularExpressions;

namespace GettingThingsMarkdown.ControlPlane;

/// <summary>Read-only proxy for encrypted historical Git objects. Authorizes by
/// authenticated account-to-namespace mapping before opening the tenant's Gitea.</summary>
public sealed class GitHistoryProxy : IDisposable
{
    private readonly HttpClient _http;
    public GitHistoryProxy(string tenantNamespace, string pem, string archivePassword)
    {
        if (string.IsNullOrWhiteSpace(pem) || string.IsNullOrEmpty(archivePassword))
            throw new InvalidOperationException("Missing history credentials.");
        var pinned = X509CertificateLoader.LoadCertificate(Encoding.UTF8.GetBytes(pem));
        var handler = new HttpClientHandler
        {
            ServerCertificateCustomValidationCallback = (_, cert, _, errors) =>
                cert is not null && (errors & SslPolicyErrors.RemoteCertificateNameMismatch) == 0 &&
                pinned.RawData.SequenceEqual(cert.RawData)
        };
        _http = new HttpClient(handler)
        {
            BaseAddress = new Uri($"https://history.{tenantNamespace}.svc.cluster.local:3000/"),
            Timeout = TimeSpan.FromSeconds(20)
        };
        var basic = Convert.ToBase64String(Encoding.UTF8.GetBytes("archive:" + archivePassword));
        _http.DefaultRequestHeaders.Authorization =
            new AuthenticationHeaderValue("Basic", basic);
    }

    public static bool ValidGitOid(string value) =>
        value.Length is 40 or 64 && Regex.IsMatch(value, "^[a-f0-9]+$",
            RegexOptions.CultureInvariant, TimeSpan.FromMilliseconds(100));

    public Task<string> RecentCommitsAsync(CancellationToken ct) =>
        GetAsync("api/v1/repos/archive/history/commits?page=1&limit=20", 2 * 1024 * 1024, ct);

    public Task<string> TreeAsync(string sha, CancellationToken ct) =>
        ValidGitOid(sha) ? GetAsync("api/v1/repos/archive/history/git/trees/" + sha + "?recursive=true", 4 * 1024 * 1024, ct)
            : throw new ArgumentException("Invalid Git object ID.");

    public Task<string> EncryptedBlobAsync(string sha, CancellationToken ct) =>
        ValidGitOid(sha) ? GetAsync("api/v1/repos/archive/history/git/blobs/" + sha, 16 * 1024 * 1024, ct)
            : throw new ArgumentException("Invalid Git object ID.");

    private async Task<string> GetAsync(string path, int maxBytes, CancellationToken ct)
    {
        using var response = await _http.GetAsync(path, HttpCompletionOption.ResponseHeadersRead, ct);
        if (!response.IsSuccessStatusCode)
            throw new HttpRequestException("Encrypted Git history is currently unavailable.",
                null, response.StatusCode);
        if (response.Content.Headers.ContentLength is { } size && size > maxBytes)
            throw new InvalidOperationException("Encrypted history response too large.");
        await using var stream = await response.Content.ReadAsStreamAsync(ct);
        using var buffer = new MemoryStream();
        var bytes = new byte[16384];
        int n;
        while ((n = await stream.ReadAsync(bytes, ct)) != 0)
        {
            if (buffer.Length + n > maxBytes)
                throw new InvalidOperationException("Encrypted history response too large.");
            buffer.Write(bytes, 0, n);
        }
        return Encoding.UTF8.GetString(buffer.ToArray());
    }

    public void Dispose() => _http.Dispose();
}
