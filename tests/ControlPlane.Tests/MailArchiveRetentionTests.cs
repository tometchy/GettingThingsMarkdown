using GettingThingsMarkdown.ControlPlane;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;

namespace GettingThingsMarkdown.Tests;

public sealed class MailArchiveRetentionTests
{
    private static readonly DateTimeOffset Now =
        new(2026, 11, 23, 12, 0, 0, TimeSpan.Zero);

    [Fact]
    public void PendingMailIsNeverEligibleForDeletion()
    {
        var account = new Account();
        var mail = new EncryptedInboundMail {
            AccountId = account.Id, ReceivedUtc = Now.AddYears(-1)
        };
        Assert.False(MailInboxPolicy.EligibleForPurge(mail, account, Now.AddYears(10)));
    }

    [Fact]
    public void AcknowledgementNeverStartsAnExpiryWindow()
    {
        var account = new Account();
        var mail = new EncryptedInboundMail {
            AccountId = account.Id, AcknowledgedUtc = Now
        };
        Assert.False(MailInboxPolicy.EligibleForPurge(mail, account, Now.AddDays(29)));
        Assert.False(MailInboxPolicy.EligibleForPurge(mail, account, Now.AddYears(10)));
    }

    [Fact]
    public void ExpiryOfHoldDoesNotDeleteMail()
    {
        var account = new Account { HistoryHoldUntilUtc = Now.AddDays(120) };
        var mail = new EncryptedInboundMail { AccountId = account.Id,
            AcknowledgedUtc = Now };
        Assert.False(MailInboxPolicy.EligibleForPurge(mail, account, Now.AddDays(100)));
        Assert.False(MailInboxPolicy.EligibleForPurge(mail, account, Now.AddDays(121)));
    }

    [Fact]
    public async Task LegacyWorkerCannotDeleteAnyMessages()
    {
        string database = Path.Combine(Path.GetTempPath(),
            "gtm-mail-sweep-" + Guid.NewGuid() + ".db");
        var options = new DbContextOptionsBuilder<AppDb>()
            .UseSqlite("Data Source=" + database).Options;
        try
        {
            using (var db = new AppDb(options))
            {
                await db.Database.EnsureCreatedAsync();
                var available = new Account { Email = "available@example.test" };
                var held = new Account { Email = "held@example.test",
                    HistoryHoldUntilUtc = Now.AddDays(60) };
                db.Accounts.AddRange(available, held);
                db.EncryptedInboundMails.AddRange(
                    new EncryptedInboundMail { AccountId = available.Id,
                        DedupSha256 = "A", ReceivedUtc = Now.AddMonths(-2),
                        AcknowledgedUtc = Now.AddDays(-31) },
                    new EncryptedInboundMail { AccountId = available.Id,
                        DedupSha256 = "B", ReceivedUtc = Now.AddYears(-1),
                        AcknowledgedUtc = null },
                    new EncryptedInboundMail { AccountId = held.Id,
                        DedupSha256 = "C", ReceivedUtc = Now.AddMonths(-2),
                        AcknowledgedUtc = Now.AddDays(-31) });
                await db.SaveChangesAsync();
            }
            var services = new Microsoft.Extensions.DependencyInjection.ServiceCollection()
                .AddDbContext<AppDb>(o => o.UseSqlite("Data Source=" + database))
                .BuildServiceProvider();
            using (services)
            {
                var scopeFactory = services.GetRequiredService<IServiceScopeFactory>();
                Assert.Equal(0,
                    await MailArchiveRetentionWorker.SweepAsync(scopeFactory, Now, default));
                using var db = new AppDb(options);
                Assert.Equal(3, await db.EncryptedInboundMails.CountAsync());
                Assert.True(await db.EncryptedInboundMails.AnyAsync(x => x.DedupSha256 == "B"));
                Assert.True(await db.EncryptedInboundMails.AnyAsync(x => x.DedupSha256 == "C"));
                Assert.Equal(0,
                    await MailArchiveRetentionWorker.SweepAsync(scopeFactory, Now, default));
                Assert.Equal(0,
                    await MailArchiveRetentionWorker.SweepAsync(
                        scopeFactory, Now.AddDays(61), default));
            }
        }
        finally
        {
            foreach (var suffix in new[] { "", "-wal", "-shm" })
                if (File.Exists(database + suffix)) File.Delete(database + suffix);
        }
    }
}
