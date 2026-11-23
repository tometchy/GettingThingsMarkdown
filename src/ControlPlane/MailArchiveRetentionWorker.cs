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
        // Never query or log the plaintext (not present); read only ACK
        // timestamps, owners and ciphertext-bearing rows for deletion.
        var acked = await db.EncryptedInboundMails
            .Where(m => m.AcknowledgedUtc != null).ToListAsync(ct);
        if (acked.Count == 0) return 0;

        var owners = acked.Select(x => x.AccountId).Distinct().ToHashSet();
        var accounts = await db.Accounts.AsNoTracking()
            .Where(a => owners.Contains(a.Id)).ToDictionaryAsync(a => a.Id, ct);
        var candidates = acked.Where(x => accounts.TryGetValue(x.AccountId, out var owner) &&
            MailInboxPolicy.EligibleForPurge(x, owner, now)).Take(500).ToList();
        if (candidates.Count == 0) return 0;
        db.EncryptedInboundMails.RemoveRange(candidates);
        await db.SaveChangesAsync(ct);
        return candidates.Count;
    }
}
