using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using GettingThingsMarkdown.ControlPlane;
using Microsoft.Extensions.Configuration;

namespace GettingThingsMarkdown.Tests;

public class StripeSignatureTests
{
    [Fact]
    public void RejectsTamperingAndStaleEvents()
    {
        const string secret = "whsec_test_secret_12345678";
        const string payload = "{\"id\":\"evt_42\",\"type\":\"checkout.session.completed\"}";
        var now = new DateTimeOffset(2026, 11, 22, 12, 0, 0, TimeSpan.Zero);
        string timestamp = now.ToUnixTimeSeconds().ToString(CultureInfo.InvariantCulture);
        string hex = Convert.ToHexString(HMACSHA256.HashData(Encoding.UTF8.GetBytes(secret),
            Encoding.UTF8.GetBytes(timestamp + "." + payload))).ToLowerInvariant();
        var config = new ConfigurationBuilder().AddInMemoryCollection(
            new Dictionary<string, string?> { ["GTM_STRIPE_WEBHOOK_SECRET"] = secret }).Build();
        var gateway = new StripeGateway(new HttpClient(), config);
        string signature = "t=" + timestamp + ",v1=" + hex;

        Assert.True(gateway.VerifySignature(payload, signature, now));
        Assert.False(gateway.VerifySignature(payload + " ", signature, now));
        Assert.False(gateway.VerifySignature(payload, signature, now.AddMinutes(6)));
        Assert.False(gateway.VerifySignature(payload, "t=nope,v1=" + hex, now));
    }
}
