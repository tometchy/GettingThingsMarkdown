using Microsoft.EntityFrameworkCore;

namespace GettingThingsMarkdown.ControlPlane;

/// <summary>
/// Acknowledgement is NOT a delete. Keep encrypted SMTP messages recoverable
/// for 30 days after local import. A tenant-wide incident hold extends that
/// deadline. Unacknowledged mail is never expired by this worker.
/// </summary>
public sealed class MailArchiveRetentionWorker(
    IServiceScopeFactory scopes, ILogger<MailArchiveRetentionWorker> logger) : BackgroundService
{
    private static readonly TimeSpan Frequency = TimeSpan.FromHours(1);

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            try { await SweepAsync(scopes, TimeProvider.System.GetUtcNow(), stoppingToken); }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
            catch (Exception ex) { logger.LogError(ex, "Encrypted mail archive cleanup failed; retry later."); }

            try { await Task.Delay(Frequency, stoppingToken); }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
        }
    }

    public static async Task<int> SweepAsync(IServiceScopeFactory scopes,
        DateTimeOffset now, CancellationToken ct)
    {
        using var scope = scopes.CreateScope();
        var db = scope.ServiceProvider.GetRequiredService<AppDb>();
        // Stream lightweight metadata; do NOT materialize every ciphertext
        // blob in memory when the global service has many customer inboxes.
        // Two queries are sequential because EF does not allow simultaneous
        // enumeration and other commands on a single DbContext.
        var accounts = await db.Accounts.AsNoTracking()
            .Select(a => new { a.Id, a.HistoryHoldUntilUtc })
            .ToDictionaryAsync(a => a.Id, ct);
        var expiredIds = new List<Guid>(500);
        await foreach (var entry in db.EncryptedInboundMails.AsNoTracking()
            .Where(m => m.AcknowledgedUtc != null)
            .Select(m => new { m.Id, m.AccountId, m.AcknowledgedUtc })
            .AsAsyncEnumerable().WithCancellation(ct))
        {
            if (entry.AcknowledgedUtc is not { } ack ||
                ack.AddDays(MailInboxPolicy.RecoveryDays) > now ||
                !accounts.TryGetValue(entry.AccountId, out var account) ||
                account.HistoryHoldUntilUtc > now)
                continue;
            expiredIds.Add(entry.Id);
            if (expiredIds.Count == 500) break;
        }
        if (expiredIds.Count == 0) return 0;
        return await db.EncryptedInboundMails
            .Where(m => expiredIds.Contains(m.Id)).ExecuteDeleteAsync(ct);
    }
}
