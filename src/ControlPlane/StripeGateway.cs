using System.Globalization;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace GettingThingsMarkdown.ControlPlane;

// The Stripe API is called server-side only. No payment keys are ever returned to the client.
public sealed class StripeGateway(HttpClient http, IConfiguration config)
{
    private string Secret => config["GTM_STRIPE_SECRET_KEY"] ??
        throw new InvalidOperationException("Stripe is not configured.");

    private string PublicUrl => (config["GTM_PUBLIC_URL"] ??
        throw new InvalidOperationException("GTM_PUBLIC_URL is required.")).TrimEnd('/');

    public bool Configured => !string.IsNullOrEmpty(config["GTM_STRIPE_SECRET_KEY"]);

    private async Task<SessionUrl> SendAsync(string endpoint, Dictionary<string, string> form, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, "https://api.stripe.com/v1/" + endpoint);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", Secret);
        request.Content = new FormUrlEncodedContent(form);
        using var response = await http.SendAsync(request, ct);
        var json = await response.Content.ReadAsStringAsync(ct);
        if (!response.IsSuccessStatusCode)
            throw new InvalidOperationException("Stripe declined this request; check server logs and configuration.");
        using var doc = JsonDocument.Parse(json);
        var url = doc.RootElement.GetProperty("url").GetString() ??
            throw new InvalidOperationException("Stripe did not return a URL.");
        if (!Uri.TryCreate(url, UriKind.Absolute, out var parsed) || parsed.Scheme != "https")
            throw new InvalidOperationException("Unexpected Stripe redirect URL.");
        return new(url);
    }

    public Task<SessionUrl> CheckoutAsync(Account account, string plan, CancellationToken ct)
    {
        string price = config["GTM_STRIPE_PRICE_" + plan.ToUpperInvariant()] ??
            throw new InvalidOperationException("Stripe price for selected plan is not configured.");
        if (!price.StartsWith("price_", StringComparison.Ordinal))
            throw new InvalidOperationException("Invalid Stripe price configuration.");
        var form = new Dictionary<string, string>
        {
            ["mode"] = "subscription",
            ["line_items[0][price]"] = price,
            ["line_items[0][quantity]"] = "1",
            ["client_reference_id"] = account.Id.ToString(),
            ["subscription_data[metadata][gtm_account_id]"] = account.Id.ToString(),
            ["success_url"] = PublicUrl + "/?billing=success",
            ["cancel_url"] = PublicUrl + "/?billing=cancel"
        };
        if (account.StripeCustomerId is { Length: > 0 } customer)
            form["customer"] = customer;
        else
            form["customer_email"] = account.Email;
        return SendAsync("checkout/sessions", form, ct);
    }

    public Task<SessionUrl> PortalAsync(Account account, CancellationToken ct)
    {
        if (string.IsNullOrEmpty(account.StripeCustomerId))
            throw new InvalidOperationException("Account has no Stripe customer yet.");
        return SendAsync("billing_portal/sessions", new()
        {
            ["customer"] = account.StripeCustomerId,
            ["return_url"] = PublicUrl + "/"
        }, ct);
    }

    public bool VerifySignature(string payload, string header, DateTimeOffset now)
    {
        string? secret = config["GTM_STRIPE_WEBHOOK_SECRET"];
        if (string.IsNullOrWhiteSpace(secret) || !secret.StartsWith("whsec_", StringComparison.Ordinal))
            return false;
        string? timestamp = null;
        var signatures = new List<string>();
        foreach (var part in header.Split(','))
        {
            string[] kv = part.Split('=', 2);
            if (kv.Length != 2) continue;
            if (kv[0] == "t") timestamp = kv[1];
            if (kv[0] == "v1") signatures.Add(kv[1]);
        }
        if (!long.TryParse(timestamp, NumberStyles.None, CultureInfo.InvariantCulture, out var unix))
            return false;
        DateTimeOffset at;
        try { at = DateTimeOffset.FromUnixTimeSeconds(unix); }
        catch (ArgumentOutOfRangeException) { return false; }
        if (Math.Abs((now - at).TotalMinutes) > 5) return false;
        byte[] expected = HMACSHA256.HashData(Encoding.UTF8.GetBytes(secret),
            Encoding.UTF8.GetBytes(timestamp + "." + payload));
        foreach (string hex in signatures)
        {
            try
            {
                byte[] actual = Convert.FromHexString(hex);
                if (CryptographicOperations.FixedTimeEquals(expected, actual)) return true;
            }
            catch (FormatException) { }
        }
        return false;
    }

    public static string? String(JsonElement element, string field)
        => element.ValueKind == JsonValueKind.Object &&
           element.TryGetProperty(field, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() : null;

    public static JsonElement Nested(JsonElement element, params string[] path)
    {
        foreach (string part in path)
        {
            if (element.ValueKind != JsonValueKind.Object ||
                !element.TryGetProperty(part, out var next)) return default;
            element = next;
        }
        return element;
    }

    public static DateTimeOffset? PeriodEnd(JsonElement subscription)
    {
        // On modern Stripe API versions the billing period is on subscription items.
        var items = Nested(subscription, "items", "data");
        if (items.ValueKind != JsonValueKind.Array) return null;
        foreach (var item in items.EnumerateArray())
        {
            var unix = Nested(item, "current_period_end");
            if (unix.ValueKind == JsonValueKind.Number && unix.TryGetInt64(out long value))
                return DateTimeOffset.FromUnixTimeSeconds(value);
        }
        return null;
    }
}
