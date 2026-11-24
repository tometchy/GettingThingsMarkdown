using Microsoft.EntityFrameworkCore;

namespace GettingThingsMarkdown.ControlPlane;

// An address is 192-bit random; it is not an authentication secret. SMTP is
// untrusted and can be spammed if the address leaks.
public sealed class MailInbox
{
    public Guid AccountId { get; set; }
    public string LocalPart { get; set; } = "";
    public string PublicKeyBase64 { get; set; } = "";
    public bool Enabled { get; set; } = true;
    public DateTimeOffset CreatedUtc { get; set; }
}

public sealed class EncryptedInboundMail
{
    public Guid Id { get; set; } = Guid.NewGuid();
    public Guid AccountId { get; set; }
    public string DedupSha256 { get; set; } = "";
    public byte[] WrappedKey { get; set; } = [];
    public byte[] Nonce { get; set; } = [];
    public byte[] Tag { get; set; } = [];
    public byte[] Ciphertext { get; set; } = [];
    public DateTimeOffset ReceivedUtc { get; set; }
    // ACK is an import receipt, NOT permission to destroy the only queued copy.
    // ACKed ciphertext stays recoverable for a separate grace period.
    public DateTimeOffset? AcknowledgedUtc { get; set; }
}

public sealed record RegisterMailInbox(string PublicKeyBase64);
public sealed record RotateMailAddress(string Password);
public sealed record MailInboxView(bool Enabled, string Address, int Pending,
    bool PublicKeyRegistered);
public sealed record MailEnvelope(Guid Id, string WrappedKey, string Nonce,
    string Tag, string Ciphertext, DateTimeOffset ReceivedUtc);
public sealed record InboundMailPayload(string Subject, string From,
    string Body, DateTimeOffset ReceivedUtc, string? RawMimeBase64 = null);

public static class MailInboxPolicy
{
    public const int MaxRawMailBytes = 40 * 1024 * 1024;
    public const int MaxPendingMessages = 200;
    public const int MaxDailyMessages = 100;
    public const int MaxRetainedMessages = 3000;
    public const int RecoveryDays = 30;

    public static DateTimeOffset RecoveryDeadline(EncryptedInboundMail item, Account account)
    {
        if (item.AcknowledgedUtc is null)
            throw new InvalidOperationException("An unacknowledged message must never expire.");
        var earliest = item.AcknowledgedUtc.Value.AddDays(RecoveryDays);
        return account.HistoryHoldUntilUtc is { } hold && hold > earliest
            ? hold : earliest;
    }

    public static bool EligibleForPurge(EncryptedInboundMail item, Account account,
        DateTimeOffset now) =>
        item.AcknowledgedUtc is not null && now >= RecoveryDeadline(item, account);


    public static bool IsEligible(Account account, DateTimeOffset now) =>
        account.Plan is "plus" or "pro" && PlanCatalog.View(account, now).CanWrite;

    public static string NewLocalPart() =>
        "r" + Convert.ToHexString(System.Security.Cryptography.RandomNumberGenerator.GetBytes(24))
            .ToLowerInvariant();

    public static bool ValidLocalPart(string value) =>
        value.Length == 49 && value[0] == 'r' &&
        value.AsSpan(1).IndexOfAnyExcept("0123456789abcdef") == -1;

    public static bool ValidRecipient(string? recipient, string? domain, out string localPart)
    {
        localPart = "";
        if (string.IsNullOrWhiteSpace(recipient) || string.IsNullOrWhiteSpace(domain) ||
            domain.Contains('@') || domain.Length > 200 ||
            !domain.All(c => char.IsAsciiLetterOrDigit(c) || c is '.' or '-'))
            return false;
        var at = recipient.LastIndexOf('@');
        if (at <= 0 || !recipient[(at + 1)..].Equals(domain, StringComparison.OrdinalIgnoreCase))
            return false;
        var local = recipient[..at];
        if (!ValidLocalPart(local)) return false;
        localPart = local;
        return true;
    }
}
