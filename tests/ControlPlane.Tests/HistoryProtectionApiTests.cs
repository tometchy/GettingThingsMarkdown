using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using GettingThingsMarkdown.ControlPlane;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;

namespace GettingThingsMarkdown.Tests;

[Collection("Integration environment")]
public sealed class HistoryProtectionApiTests
{
    [Fact]
    public async Task PasswordGatedIntentUndoAndAdminWideIncidentHoldAreAudited()
    {
        var accountsPath = Path.Combine(Path.GetTempPath(), "gtm-history-" + Guid.NewGuid() + ".db");
        var tenantsPath = Path.Combine(Path.GetTempPath(), "gtm-provision-" + Guid.NewGuid() + ".db");
        string[] keys = ["GTM_DB", "GTM_PROVISIONING_DB", "GTM_JWT_SECRET",
            "GTM_ADMIN_TOKEN", "GTM_KUBE_ENABLED"];
        var previous = keys.ToDictionary(k => k, Environment.GetEnvironmentVariable);
        try
        {
            Environment.SetEnvironmentVariable("GTM_DB", "Data Source=" + accountsPath);
            Environment.SetEnvironmentVariable("GTM_PROVISIONING_DB", "Data Source=" + tenantsPath);
            Environment.SetEnvironmentVariable("GTM_JWT_SECRET",
                "history-integration-tests-use-a-different-32-byte-secret");
            Environment.SetEnvironmentVariable("GTM_ADMIN_TOKEN", "test-admin-observability-token");
            Environment.SetEnvironmentVariable("GTM_KUBE_ENABLED", "false");

            using var factory = new WebApplicationFactory<Program>().WithWebHostBuilder(host =>
                host.UseEnvironment("Development"));
            using var user = factory.CreateClient();

            var register = await user.PostAsJsonAsync("/api/auth/register", new {
                email = "history-user@example.com",
                password = "long customer password!",
                plan = "basic"
            });
            Assert.Equal(HttpStatusCode.Created, register.StatusCode);
            var auth = await register.Content.ReadFromJsonAsync<AuthResponse>();
            Assert.NotNull(auth);
            user.DefaultRequestHeaders.Authorization =
                new AuthenticationHeaderValue("Bearer", auth.AccessToken);

            var bad = await user.PostAsJsonAsync("/api/history/deletion-requests",
                new { opaquePath = "safe/encrypted-file", password = "wrong password" });
            Assert.Equal(HttpStatusCode.Unauthorized, bad.StatusCode);
            var traversal = await user.PostAsJsonAsync("/api/history/deletion-requests",
                new { opaquePath = "../not-a-safe-path", password = "long customer password!" });
            Assert.Equal(HttpStatusCode.BadRequest, traversal.StatusCode);

            var requested = await user.PostAsJsonAsync("/api/history/deletion-requests",
                new { opaquePath = "safe/encrypted-file", password = "long customer password!" });
            Assert.Equal(HttpStatusCode.Accepted, requested.StatusCode);
            var intent = await requested.Content.ReadFromJsonAsync<HistoryDeletionView>();
            Assert.NotNull(intent);
            Assert.False(intent.PhysicalPurgeAvailable);
            Assert.True(intent.PurgeNotBeforeUtc > DateTimeOffset.UtcNow.AddDays(6));
            Assert.True(intent.PurgeNotBeforeUtc < DateTimeOffset.UtcNow.AddDays(8));

            var repeated = await user.PostAsJsonAsync("/api/history/deletion-requests",
                new { opaquePath = "safe/encrypted-file", password = "long customer password!" });
            var repeatedIntent = await repeated.Content.ReadFromJsonAsync<HistoryDeletionView>();
            Assert.Equal(HttpStatusCode.Accepted, repeated.StatusCode);
            Assert.Equal(intent.Id, repeatedIntent!.Id);
            Assert.Equal(intent.PurgeNotBeforeUtc, repeatedIntent.PurgeNotBeforeUtc);

            using var unauthorizedAdmin = await user.GetAsync("/api/admin/observability");
            Assert.Equal(HttpStatusCode.Unauthorized, unauthorizedAdmin.StatusCode);

            user.DefaultRequestHeaders.Add("X-Admin-Token", "test-admin-observability-token");
            var hold = await user.PostAsJsonAsync(
                $"/api/admin/accounts/{auth.Account.Id}/history-hold",
                new { days = 30, reason = "Encrypted files after reported compromise" });
            Assert.Equal(HttpStatusCode.OK, hold.StatusCode);
            using var held = JsonDocument.Parse(await hold.Content.ReadAsStringAsync());
            var firstUntil = held.RootElement.GetProperty("holdUntilUtc").GetDateTimeOffset();
            Assert.True(firstUntil > DateTimeOffset.UtcNow.AddDays(29));

            var shortHold = await user.PostAsJsonAsync(
                $"/api/admin/accounts/{auth.Account.Id}/history-hold",
                new { days = 1, reason = "Do not shorten protection" });
            using var preserved = JsonDocument.Parse(await shortHold.Content.ReadAsStringAsync());
            Assert.Equal(firstUntil, preserved.RootElement.GetProperty("holdUntilUtc").GetDateTimeOffset());

            var summary = await user.GetAsync("/api/admin/observability");
            Assert.Equal(HttpStatusCode.OK, summary.StatusCode);
            using var observed = JsonDocument.Parse(await summary.Content.ReadAsStringAsync());
            Assert.Equal(1, observed.RootElement.GetProperty("historyProtection")
                .GetProperty("pendingDeletionIntents").GetInt32());
            Assert.Equal(1, observed.RootElement.GetProperty("historyProtection")
                .GetProperty("activeIncidentHolds").GetInt32());
            Assert.False(observed.RootElement.GetProperty("historyProtection")
                .GetProperty("physicalPurgeEnabled").GetBoolean());
            Assert.False(observed.RootElement.GetProperty("resilience")
                .GetProperty("automaticCrossCloudFailoverEnabled").GetBoolean());

            var list = await user.GetAsync("/api/history/deletion-requests");
            Assert.Equal(HttpStatusCode.OK, list.StatusCode);
            using var body = JsonDocument.Parse(await list.Content.ReadAsStringAsync());
            Assert.Equal("safe/encrypted-file",
                body.RootElement.GetProperty("requests")[0].GetProperty("opaquePath").GetString());
            Assert.Equal(firstUntil, body.RootElement.GetProperty("requests")[0]
                .GetProperty("effectiveNotBeforeUtc").GetDateTimeOffset());

            var undo = await user.DeleteAsync($"/api/history/deletion-requests/{intent.Id}");
            Assert.Equal(HttpStatusCode.NoContent, undo.StatusCode);
            var undoTwice = await user.DeleteAsync($"/api/history/deletion-requests/{intent.Id}");
            Assert.Equal(HttpStatusCode.Conflict, undoTwice.StatusCode);

            var audit = await user.GetAsync($"/api/admin/accounts/{auth.Account.Id}/audit");
            Assert.Equal(HttpStatusCode.OK, audit.StatusCode);
            var auditBody = await audit.Content.ReadAsStringAsync();
            Assert.Contains("history-deletion-intent", auditBody);
            Assert.Contains("history-deletion-undone", auditBody);
            Assert.Contains("history-incident-hold-extended", auditBody);
            Assert.DoesNotContain("long customer password!", auditBody);
            Assert.DoesNotContain("safe/encrypted-file", auditBody);
        }
        finally
        {
            foreach (var key in keys) Environment.SetEnvironmentVariable(key, previous[key]);
            foreach (var path in new[] { accountsPath, tenantsPath })
                foreach (var suffix in new[] { "", "-wal", "-shm" })
                    if (File.Exists(path + suffix)) File.Delete(path + suffix);
        }
    }
}
