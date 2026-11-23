using GettingThingsMarkdown.ControlPlane;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;

namespace GettingThingsMarkdown.Tests;

[Collection("Integration environment")]
public sealed class MailArchiveMigrationTests
{
    [Fact]
    public async Task UpgradingExistingQueuePreservesCiphertextAndAddsNullableAckTime()
    {
        var dbPath = Path.Combine(Path.GetTempPath(), "gtm-pre-ack-" + Guid.NewGuid() + ".db");
        var provPath = Path.Combine(Path.GetTempPath(), "gtm-provision-" + Guid.NewGuid() + ".db");
        var keys = new[] { "GTM_DB", "GTM_PROVISIONING_DB", "GTM_JWT_SECRET", "GTM_KUBE_ENABLED" };
        var old = keys.ToDictionary(k => k, Environment.GetEnvironmentVariable);
        var mailId = Guid.NewGuid();
        var accountId = Guid.NewGuid();
        try
        {
            var opts = new DbContextOptionsBuilder<AppDb>()
                .UseSqlite("Data Source=" + dbPath).Options;
            using (var db = new AppDb(opts))
            {
                await db.Database.EnsureCreatedAsync();
                db.Accounts.Add(new Account {
                    Id = accountId, Email = "mail-migrate@example.test",
                    PasswordHash = "placeholder"
                });
                db.EncryptedInboundMails.Add(new EncryptedInboundMail {
                    Id = mailId, AccountId = accountId,
                    DedupSha256 = "legacy-test-hash", WrappedKey = [11, 12],
                    Nonce = [1, 2], Tag = [3, 4], Ciphertext = [5, 6, 7],
                    ReceivedUtc = new DateTimeOffset(2026, 11, 20, 0, 0, 0, TimeSpan.Zero)
                });
                await db.SaveChangesAsync();
                await db.Database.ExecuteSqlRawAsync(
                    "ALTER TABLE EncryptedInboundMails DROP COLUMN AcknowledgedUtc");
            }
            Environment.SetEnvironmentVariable("GTM_DB", "Data Source=" + dbPath);
            Environment.SetEnvironmentVariable("GTM_PROVISIONING_DB", "Data Source=" + provPath);
            Environment.SetEnvironmentVariable("GTM_JWT_SECRET",
                "this-is-a-long-enough-development-key-for-test");
            Environment.SetEnvironmentVariable("GTM_KUBE_ENABLED", "false");
            using var factory = new WebApplicationFactory<Program>()
                .WithWebHostBuilder(b => b.UseEnvironment("Development"));
            using var http = factory.CreateClient();
            Assert.True((await http.GetAsync("/readyz")).IsSuccessStatusCode);
            using var scope = factory.Services.CreateScope();
            var db2 = scope.ServiceProvider.GetRequiredService<AppDb>();
            var migrated = await db2.EncryptedInboundMails.FindAsync(mailId);
            Assert.NotNull(migrated);
            Assert.Equal(new byte[] { 5, 6, 7 }, migrated.Ciphertext);
            Assert.Null(migrated.AcknowledgedUtc);
        }
        finally
        {
            foreach (var k in keys) Environment.SetEnvironmentVariable(k, old[k]);
            foreach (var file in new[] { dbPath, provPath })
                foreach (var suffix in new[] { "", "-wal", "-shm" })
                    if (File.Exists(file + suffix)) File.Delete(file + suffix);
        }
    }
}
