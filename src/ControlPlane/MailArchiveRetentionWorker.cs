namespace GettingThingsMarkdown.ControlPlane;

/// <summary>Legacy retention entry point. Mail history has no automatic expiry.
/// Deletion requires the separate authenticated history-purge workflow.</summary>
public static class MailArchiveRetentionWorker
{
    public static Task<int> SweepAsync(IServiceScopeFactory scopes,
        DateTimeOffset now, CancellationToken ct) => Task.FromResult(0);
}
