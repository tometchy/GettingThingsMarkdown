using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;

namespace GettingThingsMarkdown.Client.Shared;

public record AccountInfo(Guid Id, string Email, string Plan, long QuotaBytes,
    DateTimeOffset TrialUntilUtc, DateTimeOffset? PaidUntilUtc,
    string PaymentStatus, bool CanWrite, string Entitlement);
public record LoginResult(string AccessToken, AccountInfo Account);
public record CheckoutUrl(string Url);

public sealed class AccountApi : IDisposable
{
    private readonly HttpClient _http = new() { BaseAddress = ApiConfiguration.BaseAddress };
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);

    private async Task<T> RequestAsync<T>(string path, object? payload = null)
    {
        using var request = new HttpRequestMessage(payload is null ? HttpMethod.Get : HttpMethod.Post, path);
        if (payload is not null) request.Content = JsonContent.Create(payload);
        using var response = await _http.SendAsync(request);
        if (!response.IsSuccessStatusCode)
            throw new HttpRequestException($"HTTP {(int)response.StatusCode}: {await response.Content.ReadAsStringAsync()}");
        return await response.Content.ReadFromJsonAsync<T>(JsonOptions)
            ?? throw new InvalidOperationException("Empty response.");
    }

    public async Task<AccountInfo> RegisterAsync(string email, string password, string plan)
    {
        var result = await RequestAsync<LoginResult>("api/auth/register", new { email, password, plan });
        SetToken(result.AccessToken);
        return result.Account;
    }

    public async Task<AccountInfo> LoginAsync(string email, string password)
    {
        var result = await RequestAsync<LoginResult>("api/auth/login", new { email, password });
        SetToken(result.AccessToken);
        return result.Account;
    }

    private void SetToken(string token) =>
        _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);

    public Task<AccountInfo> CurrentAsync() => RequestAsync<AccountInfo>("api/account");
    public async Task<Uri> CheckoutAsync(string plan)
        => new((await RequestAsync<CheckoutUrl>("api/billing/checkout", new { plan })).Url);
    public async Task<Uri> PortalAsync()
        => new((await RequestAsync<CheckoutUrl>("api/billing/portal", new { })).Url);

    public void Dispose() => _http.Dispose();
}
