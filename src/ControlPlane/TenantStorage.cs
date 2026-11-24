using System.Net.Http.Json;
namespace GettingThingsMarkdown.ControlPlane;

public sealed record TenantStorageView(long LiveBytes, long HistoryBytes, long QuotaBytes,
    long HistoryBudgetBytes, long FreeBytes, long ReserveBytes, bool Blocked, string[] Reasons);

public sealed class TenantStorage : IDisposable
{
    private readonly HttpClient http;
    public TenantStorage(string ns, string key)
    {
        http = new HttpClient { BaseAddress = new Uri($"http://sync.{ns}.svc.cluster.local:8385/"),
            Timeout = TimeSpan.FromSeconds(25) };
        http.DefaultRequestHeaders.Add("X-API-Key", key);
    }

    public async Task SetQuotaAsync(long quota, CancellationToken ct)
    {
        using var result = await http.PostAsJsonAsync("quota", new { quotaBytes = quota }, ct);
        result.EnsureSuccessStatusCode();
    }

    public async Task<TenantStorageView> UsageAsync(CancellationToken ct) =>
        await http.GetFromJsonAsync<TenantStorageView>("usage", ct)
            ?? throw new InvalidOperationException("Storage usage unavailable.");

    public void Dispose() => http.Dispose();
}
