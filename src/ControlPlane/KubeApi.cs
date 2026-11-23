using System.Net;
using System.Net.Http.Headers;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;

namespace GettingThingsMarkdown.ControlPlane;

/// <summary>Small Kubernetes API client for a single in-cluster reconciler.
/// Server-side apply makes the declared resources idempotent.</summary>
public sealed class KubeApi : IDisposable
{
    private readonly HttpClient _http;
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);
    public static bool Available =>
        !string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("KUBERNETES_SERVICE_HOST"));

    public KubeApi()
    {
        string host = Environment.GetEnvironmentVariable("KUBERNETES_SERVICE_HOST")
            ?? throw new InvalidOperationException("Not running inside Kubernetes.");
        string port = Environment.GetEnvironmentVariable("KUBERNETES_SERVICE_PORT") ?? "443";
        const string path = "/var/run/secrets/kubernetes.io/serviceaccount/";
        string token = File.ReadAllText(path + "token").Trim();
        var ca = X509CertificateLoader.LoadCertificateFromFile(path + "ca.crt");
        var handler = new HttpClientHandler
        {
            ServerCertificateCustomValidationCallback = (_, cert, _, sslErrors) =>
            {
                if (cert is null || (sslErrors & SslPolicyErrors.RemoteCertificateNameMismatch) != 0)
                    return false;
                using var chain = new X509Chain();
                chain.ChainPolicy.TrustMode = X509ChainTrustMode.CustomRootTrust;
                chain.ChainPolicy.CustomTrustStore.Add(ca);
                chain.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;
                return chain.Build(new X509Certificate2(cert));
            }
        };
        _http = new HttpClient(handler)
        {
            BaseAddress = new Uri($"https://{host}:{port}"),
            Timeout = TimeSpan.FromSeconds(20)
        };
        _http.DefaultRequestHeaders.Authorization =
            new AuthenticationHeaderValue("Bearer", token);
    }

    public async Task ApplyAsync(KubeObject obj, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Patch,
            obj.Url + "?fieldManager=gtm-controller&force=true");
        request.Content = new StringContent(obj.Json, Encoding.UTF8);
        request.Content.Headers.ContentType =
            new MediaTypeHeaderValue("application/apply-patch+yaml");
        using var response = await _http.SendAsync(request, ct);
        if (!response.IsSuccessStatusCode)
            throw new InvalidOperationException(
                $"Kubernetes apply {obj.Kind}/{obj.Name}: {(int)response.StatusCode} " +
                (await response.Content.ReadAsStringAsync(ct)).Substring(0, 300));
    }

    public async Task<JsonDocument?> GetAsync(string url, CancellationToken ct)
    {
        using var response = await _http.GetAsync(url, ct);
        if (response.StatusCode == HttpStatusCode.NotFound) return null;
        response.EnsureSuccessStatusCode();
        return JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct));
    }

    public async Task<bool> StatefulSetReadyAsync(string ns, string name, CancellationToken ct)
    {
        using var doc = await GetAsync($"/apis/apps/v1/namespaces/{ns}/statefulsets/{name}", ct);
        if (doc is null) return false;
        if (!doc.RootElement.TryGetProperty("status", out var status)) return false;
        return status.TryGetProperty("readyReplicas", out var replicas) && replicas.GetInt32() == 1;
    }

    public static long QuantityGiB(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) throw new ArgumentException("Empty Kubernetes storage quantity.");
        long bytes;
        if (value.EndsWith("Gi", StringComparison.Ordinal))
            bytes = checked(long.Parse(value[..^2], System.Globalization.CultureInfo.InvariantCulture) *
                1024L * 1024 * 1024);
        else if (value.EndsWith("Mi", StringComparison.Ordinal))
            bytes = checked(long.Parse(value[..^2], System.Globalization.CultureInfo.InvariantCulture) *
                1024L * 1024);
        else if (value.EndsWith("Ti", StringComparison.Ordinal))
            bytes = checked(long.Parse(value[..^2], System.Globalization.CultureInfo.InvariantCulture) *
                1024L * 1024 * 1024 * 1024);
        else throw new ArgumentException("Unsupported Kubernetes storage quantity suffix.");
        return (bytes + (1024L * 1024 * 1024 - 1)) / (1024L * 1024 * 1024);
    }

    public async Task<bool> ExpandPvcIfNeededAsync(KubeObject desired, CancellationToken ct)
    {
        if (desired.Kind != "PersistentVolumeClaim" ||
            desired.Namespace is null) throw new ArgumentException("Expected namespaced PVC.");
        using var current = await GetAsync(desired.Url, ct);
        if (current is null) throw new InvalidOperationException("PVC not found.");
        using var requested = JsonDocument.Parse(desired.Json);
        string oldQuantity = current.RootElement.GetProperty("spec")
            .GetProperty("resources").GetProperty("requests").GetProperty("storage").GetString()!;
        string newQuantity = requested.RootElement.GetProperty("spec")
            .GetProperty("resources").GetProperty("requests").GetProperty("storage").GetString()!;
        if (QuantityGiB(newQuantity) <= QuantityGiB(oldQuantity))
            return false; // Never shrink persistent encrypted filesystem: Kubernetes forbids it.
        await ApplyAsync(desired, ct);
        return true;
    }

    public async Task<bool> ExistsAsync(string url, CancellationToken ct)
    {
        using var obj = await GetAsync(url, ct);
        return obj is not null;
    }

    public async Task<Dictionary<string, string>?> SecretAsync(string ns, string name, CancellationToken ct)
    {
        using var doc = await GetAsync($"/api/v1/namespaces/{ns}/secrets/{name}", ct);
        if (doc is null) return null;
        var data = doc.RootElement.GetProperty("data");
        return data.EnumerateObject().ToDictionary(x => x.Name,
            x => Encoding.UTF8.GetString(Convert.FromBase64String(x.Value.GetString() ?? "")));
    }

    public void Dispose() => _http.Dispose();
}

public sealed record KubeObject(string ApiVersion, string Kind, string Name,
    string? Namespace, string Json)
{
    public string Url
    {
        get
        {
            var prefix = ApiVersion == "v1" ? "/api/v1" : "/apis/" + ApiVersion;
            string resource = Kind switch
            {
                "Namespace" => "namespaces",
                "Secret" => "secrets",
                "Service" => "services",
                "PersistentVolumeClaim" => "persistentvolumeclaims",
                "StatefulSet" => "statefulsets",
                "NetworkPolicy" => "networkpolicies",
                "Certificate" => "certificates",
                _ => throw new NotSupportedException(Kind)
            };
            return prefix + (Namespace is null ? "" : "/namespaces/" + Namespace) +
                "/" + resource + "/" + Name;
        }
    }

    public static KubeObject From(string apiVersion, string kind, string name,
        string? ns, object specOrBody)
    {
        var payload = System.Text.Json.JsonSerializer.Serialize(specOrBody,
            new JsonSerializerOptions(JsonSerializerDefaults.Web));
        return new(apiVersion, kind, name, ns, payload);
    }
}
