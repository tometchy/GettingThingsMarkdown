using System.Net;
using System.Net.Http;
using System.Text;
using GettingThingsMarkdown.ControlPlane;
using Microsoft.Extensions.Configuration;

namespace GettingThingsMarkdown.Tests;

public sealed class PricingTests
{
    [Theory]
    [InlineData("basic", 10L, 199, 1788)]
    [InlineData("plus", 75L, 599, 5988)]
    [InlineData("pro", 500L, 2499, 23988)]
    public void ExactlyThreePublishedTiersHaveExpectedQuotasAndPrices(
        string id, long gib, int monthly, int yearly)
    {
        Assert.Equal(3, PlanCatalog.Tiers.Count);
        var tier = PlanCatalog.Tiers[id];
        Assert.Equal(gib * 1024 * 1024 * 1024, tier.QuotaBytes);
        Assert.Equal(monthly, tier.PriceCents("month"));
        Assert.Equal(yearly, tier.PriceCents("year"));
        Assert.False(PlanCatalog.ValidInterval("week"));
    }

    [Fact]
    public async Task CheckoutRejectsWrongStripePriceBeforeSendingPaymentUrl()
    {
        var handler = new FakeStripeHandler(400, "month");
        using var http = new HttpClient(handler);
        var gateway = Gateway(http);
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            gateway.CheckoutAsync(Account(), "plus", "month", CancellationToken.None));
        Assert.Equal(0, handler.CheckoutCalls);
    }

    [Fact]
    public async Task CheckoutAcceptsExactAnnualPriceAndCreatesValidSession()
    {
        var handler = new FakeStripeHandler(5988, "year");
        using var http = new HttpClient(handler);
        var gateway = Gateway(http);
        var result = await gateway.CheckoutAsync(Account(), "plus", "year",
            CancellationToken.None);
        Assert.Equal("https://checkout.stripe.com/c/pay/test", result.Url);
        Assert.Equal(1, handler.CheckoutCalls);
        Assert.Contains("price_plus_year", handler.CheckoutBody);
    }

    [Fact]
    public async Task CheckoutRejectsIncorrectIntervalEvenWhenAmountMatches()
    {
        var handler = new FakeStripeHandler(5988, "month");
        using var http = new HttpClient(handler);
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            Gateway(http).CheckoutAsync(Account(), "plus", "year", CancellationToken.None));
        Assert.Equal(0, handler.CheckoutCalls);
    }

    [Fact]
    public void GrandfatheredQuotasWinOverNewPlanSizes()
    {
        var account = Account();
        account.QuotaOverrideBytes = 200L * 1024 * 1024 * 1024;
        Assert.Equal(200L * 1024 * 1024 * 1024,
            PlanCatalog.View(account, DateTimeOffset.UtcNow).QuotaBytes);
        account.QuotaOverrideBytes = null;
        Assert.Equal(75L * 1024 * 1024 * 1024,
            PlanCatalog.View(account, DateTimeOffset.UtcNow).QuotaBytes);
    }

    private static Account Account() => new()
    {
        Id = Guid.NewGuid(), Email = "test@example.invalid", Plan = "plus"
    };

    private static StripeGateway Gateway(HttpClient http)
    {
        var configuration = new ConfigurationBuilder().AddInMemoryCollection(
            new Dictionary<string, string?>
            {
                ["GTM_STRIPE_SECRET_KEY"] = "sk_test_placeholder_nonlive",
                ["GTM_PUBLIC_URL"] = "https://app.example.invalid",
                ["GTM_STRIPE_PRICE_PLUS_MONTH"] = "price_plus_month",
                ["GTM_STRIPE_PRICE_PLUS_YEAR"] = "price_plus_year"
            }).Build();
        return new StripeGateway(http, configuration);
    }

    private sealed class FakeStripeHandler(int cents, string interval) : HttpMessageHandler
    {
        public int CheckoutCalls { get; private set; }
        public string CheckoutBody { get; private set; } = "";
        protected override async Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken ct)
        {
            if (request.Method == HttpMethod.Get &&
                request.RequestUri!.AbsolutePath.Contains("/prices/", StringComparison.Ordinal))
                return new(HttpStatusCode.OK)
                {
                    Content = new StringContent(
                        "{\"id\":\"price_test\",\"currency\":\"usd\",\"active\":true,\"unit_amount\":" +
                        cents + ",\"recurring\":{\"interval\":\"" + interval + "\"}}",
                        Encoding.UTF8, "application/json")
                };
            if (request.Method == HttpMethod.Post &&
                request.RequestUri!.AbsolutePath.EndsWith("/checkout/sessions", StringComparison.Ordinal))
            {
                CheckoutCalls++;
                CheckoutBody = await request.Content!.ReadAsStringAsync(ct);
                return new(HttpStatusCode.OK)
                {
                    Content = new StringContent(
                        "{\"url\":\"https://checkout.stripe.com/c/pay/test\"}",
                        Encoding.UTF8, "application/json")
                };
            }
            throw new InvalidOperationException("Unexpected Stripe request.");
        }
    }
}
