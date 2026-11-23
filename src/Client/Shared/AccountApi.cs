using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json.Serialization;
using System.Text.Json.Serialization.Metadata;

namespace GettingThingsMarkdown.Client.Shared;

public record AccountInfo(Guid Id, string Email, string Plan, long QuotaBytes,
    DateTimeOffset TrialUntilUtc, DateTimeOffset? PaidUntilUtc,
    string PaymentStatus, bool CanWrite, string Entitlement);
public record LoginResult(string AccessToken, AccountInfo Account);
public record CheckoutUrl(string Url);
public record RegisterPayload(string Email, string Password, string Plan);
public record LoginPayload(string Email, string Password);
public record CheckoutPayload(string Plan, string BillingInterval = "month");
public record PairDevicePayload(string DeviceId, string Name);
public record TenantStatusInfo(string Status, string? ServerDeviceId, string? Detail,
    string FolderId, string? LocalSyncAddress);

// WASM trimming and future iOS AOT: all API JSON shapes must be source-generated.
[JsonSourceGenerationOptions(PropertyNamingPolicy = JsonKnownNamingPolicy.CamelCase)]
[JsonSerializable(typeof(AccountInfo))]
[JsonSerializable(typeof(LoginResult))]
[JsonSerializable(typeof(CheckoutUrl))]
[JsonSerializable(typeof(RegisterPayload))]
[JsonSerializable(typeof(LoginPayload))]
[JsonSerializable(typeof(CheckoutPayload))]
[JsonSerializable(typeof(PairDevicePayload))]
[JsonSerializable(typeof(TenantStatusInfo))]
[JsonSerializable(typeof(ServerPairing))]
[JsonSerializable(typeof(string[]))]
public partial class AccountJsonContext : JsonSerializerContext { }

public sealed class AccountApi : IDisposable
{
    private readonly HttpClient _http = new() { BaseAddress = ApiConfiguration.BaseAddress };

    private async Task<T> RequestAsync<T>(HttpMethod method, string path,
        HttpContent? payload, JsonTypeInfo<T> responseType)
    {
        using var request = new HttpRequestMessage(method, path) { Content = payload };
        using var response = await _http.SendAsync(request);
        if (!response.IsSuccessStatusCode)
            throw new HttpRequestException($"HTTP {(int)response.StatusCode}: {await response.Content.ReadAsStringAsync()}");
        return await response.Content.ReadFromJsonAsync(responseType)
            ?? throw new InvalidOperationException("Empty response.");
    }

    public async Task<AccountInfo> RegisterAsync(string email, string password, string plan)
    {
        using var payload = JsonContent.Create(
            new RegisterPayload(email, password, plan), AccountJsonContext.Default.RegisterPayload);
        var result = await RequestAsync(HttpMethod.Post, "api/auth/register",
            payload, AccountJsonContext.Default.LoginResult);
        SetToken(result.AccessToken);
        return result.Account;
    }

    public async Task<AccountInfo> LoginAsync(string email, string password)
    {
        using var payload = JsonContent.Create(
            new LoginPayload(email, password), AccountJsonContext.Default.LoginPayload);
        var result = await RequestAsync(HttpMethod.Post, "api/auth/login",
            payload, AccountJsonContext.Default.LoginResult);
        SetToken(result.AccessToken);
        return result.Account;
    }

    private void SetToken(string token) =>
        _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);

    public Task<AccountInfo> CurrentAsync() =>
        RequestAsync(HttpMethod.Get, "api/account", null, AccountJsonContext.Default.AccountInfo);

    public async Task<Uri> CheckoutAsync(string plan, string billingInterval = "month")
    {
        using var payload = JsonContent.Create(new CheckoutPayload(plan, billingInterval),
            AccountJsonContext.Default.CheckoutPayload);
        var result = await RequestAsync(HttpMethod.Post, "api/billing/checkout",
            payload, AccountJsonContext.Default.CheckoutUrl);
        return new Uri(result.Url);
    }

    public async Task<Uri> PortalAsync()
    {
        var result = await RequestAsync(HttpMethod.Post, "api/billing/portal",
            null, AccountJsonContext.Default.CheckoutUrl);
        return new Uri(result.Url);
    }

    public Task<TenantStatusInfo> TenantAsync() =>
        RequestAsync(HttpMethod.Get, "api/tenant", null, AccountJsonContext.Default.TenantStatusInfo);

    public async Task<ServerPairing> PairDeviceAsync(string deviceId, string name)
    {
        using var payload = JsonContent.Create(new PairDevicePayload(deviceId, name),
            AccountJsonContext.Default.PairDevicePayload);
        return await RequestAsync(HttpMethod.Post, "api/devices",
            payload, AccountJsonContext.Default.ServerPairing);
    }

    public Task<string[]> DevicesAsync() =>
        RequestAsync(HttpMethod.Get, "api/devices", null, AccountJsonContext.Default.StringArray);

    public async Task RevokeDeviceAsync(string deviceId)
    {
        using var result = await _http.DeleteAsync("api/devices/" + Uri.EscapeDataString(deviceId));
        result.EnsureSuccessStatusCode();
    }

    public async Task<string> EncryptedHistoryJsonAsync(string path, CancellationToken ct = default)
    {
        if (path != "commits" && !(path.StartsWith("tree/", StringComparison.Ordinal) ||
                                   path.StartsWith("blob/", StringComparison.Ordinal)))
            throw new ArgumentException("Invalid history resource.");
        using var response = await _http.GetAsync("api/history/" + path, ct);
        response.EnsureSuccessStatusCode();
        // Deliberately return raw encrypted JSON; don't deserialize arbitrary remote
        // Git payloads into runtime-reflection-bound object graphs (iOS/WASM AOT).
        return await response.Content.ReadAsStringAsync(ct);
    }

    public void Dispose() => _http.Dispose();
}
