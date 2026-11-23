using System.Net;
using System.Net.Http.Json;
using GettingThingsMarkdown.ControlPlane;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.AspNetCore.Identity;
using Microsoft.EntityFrameworkCore;

namespace GettingThingsMarkdown.Tests;

[Collection("Integration environment")]
public sealed class LegacyQuotaMigrationTests
{
    [Fact]
    public async Task ExistingFifteenTwoHundredAndThousandTwentyFourGiBUsersKeepTheirQuotas()
    {
        var file = Path.Combine(Path.GetTempPath(), "gtm-old-" + Guid.NewGuid() + ".sqlite");
        var provFile = Path.Combine(Path.GetTempPath(), "gtm-prov-" + Guid.NewGuid() + ".sqlite");
        var keys = new[] { "GTM_DB", "GTM_PROVISIONING_DB", "GTM_JWT_SECRET", "GTM_KUBE_ENABLED" };
        var old = keys.ToDictionary(x => x, Environment.GetEnvironmentVariable);
        try
        {
            var options = new DbContextOptionsBuilder<AppDb>()
                .UseSqlite("Data Source=" + file).Options;
            using (var db = new AppDb(options))
            {
                await db.Database.EnsureCreatedAsync();
                foreach (var plan in new[] { "basic", "plus", "pro" })
                {
                    var account = new Account
                    {
                        Plan = plan, Email = plan + "@legacy.invalid",
                        CreatedUtc = DateTimeOffset.UtcNow.AddMonths(-2),
                        TrialUntilUtc = DateTimeOffset.UtcNow.AddMonths(1)
                    };
                    account.PasswordHash = new PasswordHasher<Account>()
                        .HashPassword(account, "a very long testing password");
                    db.Accounts.Add(account);
                }
                await db.SaveChangesAsync();
                // Simulate a valid historical app schema before the new column existed.
                await db.Database.ExecuteSqlRawAsync(
                    "ALTER TABLE Accounts DROP COLUMN QuotaOverrideBytes");
            }
            Environment.SetEnvironmentVariable("GTM_DB", "Data Source=" + file);
            Environment.SetEnvironmentVariable("GTM_PROVISIONING_DB", "Data Source=" + provFile);
            Environment.SetEnvironmentVariable("GTM_JWT_SECRET",
                "legacy-migration-test-uses-a-unique-key-over-32-bytes");
            Environment.SetEnvironmentVariable("GTM_KUBE_ENABLED", "false");

            for (var restart = 0; restart < 2; restart++)
            {
                using var app = new WebApplicationFactory<Program>()
                    .WithWebHostBuilder(host => host.UseEnvironment("Development"));
                using var http = app.CreateClient();
                foreach (var (id, expected) in new (string, long)[] {
                    ("basic", 15), ("plus", 200), ("pro", 1024) })
                {
                    using var response = await http.PostAsJsonAsync("/api/auth/login",
                        new { email = id + "@legacy.invalid",
                              password = "a very long testing password" });
                    Assert.Equal(HttpStatusCode.OK, response.StatusCode);
                    var login = await response.Content.ReadFromJsonAsync<AuthResponse>();
                    Assert.NotNull(login);
                    Assert.Equal(expected * 1024 * 1024 * 1024,
                        login.Account.QuotaBytes);
                }
                var plans = await http.GetStringAsync("/api/plans");
                Assert.Contains("\"quotaBytes\":80530636800", plans); // 75 GiB new Plus
            }
        }
        finally
        {
            foreach (var key in keys)
                Environment.SetEnvironmentVariable(key, old[key]);
            foreach (var fileName in new[] { file, provFile })
                foreach (var suffix in new[] { "", "-wal", "-shm" })
                    if (File.Exists(fileName + suffix)) File.Delete(fileName + suffix);
        }
    }
}
