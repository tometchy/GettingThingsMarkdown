using System.Net;
using System.Net.Http.Json;
using GettingThingsMarkdown.ControlPlane;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;

namespace GettingThingsMarkdown.Tests;

// WebApplicationFactory starts Program before ConfigureAppConfiguration callbacks
// are applied; startup secrets must therefore be present in process environment.
[Collection("Integration environment")]
public sealed class RegistrationTests
{
    private static WebApplicationFactory<Program> CreateFactory() =>
        new WebApplicationFactory<Program>().WithWebHostBuilder(host =>
        {
            host.UseEnvironment("Development");
        });

    [Theory]
    [InlineData(false, 1)]
    [InlineData(true, 1200)]
    public async Task RegistrationCreatesAccountAndPreventsCrossAccountAccess(bool longTrial, int minimumDays)
    {
        var db = Path.Combine(Path.GetTempPath(), "gtm-" + Guid.NewGuid() + ".db");
        string[] keys = ["GTM_DB", "GTM_JWT_SECRET", "GTM_ADMIN_TOKEN", "GTM_DEV_LONG_TRIAL"];
        var previous = keys.ToDictionary(x => x, Environment.GetEnvironmentVariable);
        try
        {
            Environment.SetEnvironmentVariable("GTM_DB", "Data Source=" + db);
            Environment.SetEnvironmentVariable("GTM_JWT_SECRET", "integration-tests-have-a-secret-over-32-characters");
            Environment.SetEnvironmentVariable("GTM_ADMIN_TOKEN", "integration-admin-only-secret");
            Environment.SetEnvironmentVariable("GTM_DEV_LONG_TRIAL", longTrial.ToString());

            using var factory = CreateFactory();
            using var client = factory.CreateClient();
            var first = await client.PostAsJsonAsync("/api/auth/register",
                new { email = "first@example.com", password = "a long and safe password", plan = "plus" });
            Assert.Equal(HttpStatusCode.Created, first.StatusCode);
            var result = await first.Content.ReadFromJsonAsync<AuthResponse>();
            Assert.NotNull(result);
            Assert.True(result.Account.EmailVerified);
            Assert.Equal(75L * 1024 * 1024 * 1024, result.Account.QuotaBytes);
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

            client.DefaultRequestHeaders.Add("X-Admin-Token", "integration-admin-only-secret");
            var granted = await client.PostAsJsonAsync(
                $"/api/admin/accounts/{own.Id}/extend-trial", new { days = 5, reason = "support" });
            Assert.Equal(HttpStatusCode.OK, granted.StatusCode);
            var extended = await granted.Content.ReadFromJsonAsync<AccountView>();
            Assert.True(extended!.TrialUntilUtc > own.TrialUntilUtc.AddDays(4));
        }
        finally
        {
            foreach (var key in keys) Environment.SetEnvironmentVariable(key, previous[key]);
            foreach (var suffix in new[] { "", "-wal", "-shm" })
                if (File.Exists(db + suffix)) File.Delete(db + suffix);
        }
    }
}

[CollectionDefinition("Integration environment", DisableParallelization = true)]
public sealed class IntegrationEnvironmentCollection { }
