using System.Net;
using System.Net.Http.Json;
using GettingThingsMarkdown.ControlPlane;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.Configuration;

namespace GettingThingsMarkdown.Tests;

public sealed class RegistrationTests
{
    private static WebApplicationFactory<Program> CreateFactory(string dbPath, bool longTrial) =>
        new WebApplicationFactory<Program>().WithWebHostBuilder(host =>
        {
            host.UseEnvironment("Development");
            host.ConfigureAppConfiguration((_, cfg) => cfg.AddInMemoryCollection(
                new Dictionary<string, string?>
                {
                    ["GTM_JWT_SECRET"] = "integration-tests-have-a-secret-over-32-characters",
                    ["GTM_DB"] = "Data Source=" + dbPath,
                    ["GTM_DEV_LONG_TRIAL"] = longTrial.ToString(),
                    ["GTM_ADMIN_TOKEN"] = "integration-admin-only-secret"
                }));
        });

    [Theory]
    [InlineData(false, 1)]
    [InlineData(true, 1200)]
    public async Task RegistrationCreatesAccountAndPreventsCrossAccountAccess(bool longTrial, int minimumDays)
    {
        var db = Path.Combine(Path.GetTempPath(), "gtm-" + Guid.NewGuid() + ".db");
        try
        {
            using var factory = CreateFactory(db, longTrial);
            using var client = factory.CreateClient();
            var first = await client.PostAsJsonAsync("/api/auth/register",
                new { email = "first@example.com", password = "a long and safe password", plan = "plus" });
            Assert.Equal(HttpStatusCode.Created, first.StatusCode);
            var result = await first.Content.ReadFromJsonAsync<AuthResponse>();
            Assert.NotNull(result);
            Assert.True((result.Account.TrialUntilUtc - DateTimeOffset.UtcNow).TotalDays > minimumDays - 1);

            var duplicate = await client.PostAsJsonAsync("/api/auth/register",
                new { email = "FIRST@example.com", password = "a long and safe password", plan = "plus" });
            Assert.Equal(HttpStatusCode.Conflict, duplicate.StatusCode);
            Assert.Equal(HttpStatusCode.Unauthorized, (await client.GetAsync("/api/account")).StatusCode);

            client.DefaultRequestHeaders.Authorization =
                new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", result.AccessToken);
            var own = await client.GetFromJsonAsync<AccountView>("/api/account");
            Assert.Equal(result.Account.Id, own!.Id);

            var forbidden = await client.PostAsJsonAsync(
                $"/api/admin/accounts/{own.Id}/extend-trial", new { days = 5, reason = "test" });
            Assert.Equal(HttpStatusCode.Unauthorized, forbidden.StatusCode);
        }
        finally
        {
            foreach (var suffix in new[] { "", "-wal", "-shm" })
                if (File.Exists(db + suffix)) File.Delete(db + suffix);
        }
    }
}
