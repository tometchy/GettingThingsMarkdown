using System.Security.Cryptography;
using System.Text;

namespace GettingThingsMarkdown.ControlPlane;

/// <summary>
/// A durable, account-owned request to remove an opaque encrypted path from Git
/// history. An intent NEVER erases Git objects. A separately verified immutable
/// retention and dual-replica purge process is required before completing it.
/// </summary>
public sealed class HistoryDeletionIntent
{
    public Guid Id { get; set; } = Guid.NewGuid();
    public Guid AccountId { get; set; }
    public string OpaquePath { get; set; } = "";
    public string State { get; set; } = "Pending";
    public DateTimeOffset RequestedUtc { get; set; }
    public DateTimeOffset PurgeNotBeforeUtc { get; set; }
    public DateTimeOffset? CanceledUtc { get; set; }
}

public sealed record RequestHistoryDeletion(string OpaquePath, string Password);
public sealed record HistoryProtectionHoldRequest(int Days, string Reason);

/// <summary>Exposes only the caller's opaque encrypted path, never plaintext content.</summary>
public sealed record HistoryDeletionView(
    Guid Id, string OpaquePath, string State, DateTimeOffset RequestedUtc,
    DateTimeOffset PurgeNotBeforeUtc, DateTimeOffset EffectiveNotBeforeUtc,
    bool PhysicalPurgeAvailable);

public static class HistoryProtectionPolicy
{
    public const int InitialRetentionDays = 7;
    public const int MaxIncidentHoldDays = 365;

    public static bool ValidOpaquePath(string? path)
    {
        if (string.IsNullOrWhiteSpace(path) || Encoding.UTF8.GetByteCount(path) > 1024 ||
            path.StartsWith('/') || path.Contains('\\') || path.Any(char.IsControl))
            return false;
        // Must be relative, canonical and safe for a future Git rewrite. This
        // path is opaque server-side: it is NOT a user's plaintext filename.
        return path.Split('/').All(part => part.Length > 0 && part is not ("." or ".."));
    }

    public static HistoryDeletionIntent New(Guid owner, string opaquePath, DateTimeOffset now)
    {
        if (!ValidOpaquePath(opaquePath))
            throw new ArgumentException("Invalid encrypted relative path.", nameof(opaquePath));
        return new HistoryDeletionIntent
        {
            AccountId = owner,
            OpaquePath = opaquePath,
            State = "Pending",
            RequestedUtc = now,
            PurgeNotBeforeUtc = now.AddDays(InitialRetentionDays)
        };
    }

    public static void RestartCanceled(HistoryDeletionIntent intent, DateTimeOffset now)
    {
        if (intent.State != "Canceled")
            throw new InvalidOperationException("Only canceled requests can be restarted.");
        intent.State = "Pending";
        intent.RequestedUtc = now;
        intent.PurgeNotBeforeUtc = now.AddDays(InitialRetentionDays);
        intent.CanceledUtc = null;
    }

    public static void Cancel(HistoryDeletionIntent intent, DateTimeOffset now)
    {
        if (intent.State != "Pending")
            throw new InvalidOperationException("Request cannot be undone.");
        intent.State = "Canceled";
        intent.CanceledUtc = now;
    }

    public static DateTimeOffset ExtendTenantHold(Account account, DateTimeOffset now, int days)
    {
        if (days is < 1 or > MaxIncidentHoldDays)
            throw new ArgumentOutOfRangeException(nameof(days));
        var proposed = now.AddDays(days);
        if (account.HistoryHoldUntilUtc is { } existing && existing > proposed)
            return existing; // An administrator cannot shorten a previously promised hold.
        account.HistoryHoldUntilUtc = proposed;
        return proposed;
    }

    public static DateTimeOffset EffectiveNotBefore(HistoryDeletionIntent intent, Account account)
        => account.HistoryHoldUntilUtc is { } hold && hold > intent.PurgeNotBeforeUtc
            ? hold : intent.PurgeNotBeforeUtc;

    public static string Fingerprint(string opaquePath)
        => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(opaquePath)))[..20];
}
