using Microsoft.EntityFrameworkCore;

namespace GettingThingsMarkdown.ControlPlane;

public sealed class Account
{
    public Guid Id { get; set; } = Guid.NewGuid();
    public string Email { get; set; } = "";
    public string PasswordHash { get; set; } = "";
    public string Plan { get; set; } = "basic";
    // Persist old subscriber allowances when introducing a smaller plan catalog.
    public long? QuotaOverrideBytes { get; set; }
    // Temporary administrator-granted relief; no payment or plan change.
    public int GiftQuotaGiB { get; set; }
    public DateTimeOffset? GiftQuotaExpiresUtc { get; set; }
    // Incident hold applies to the entire tenant and can only be extended.
    public DateTimeOffset? HistoryHoldUntilUtc { get; set; }
    public DateTimeOffset? EmailVerifiedUtc { get; set; }
    public string? VerificationTokenHash { get; set; }
    public DateTimeOffset? VerificationExpiresUtc { get; set; }
    public DateTimeOffset? VerificationSentUtc { get; set; }
    public DateTimeOffset CreatedUtc { get; set; }
    public DateTimeOffset TrialUntilUtc { get; set; }
    public DateTimeOffset? PaidUntilUtc { get; set; }
    public string? StripeCustomerId { get; set; }
    public string? StripeSubscriptionId { get; set; }
    public string StripeStatus { get; set; } = "none";
}

public sealed class AuditEntry
{
    public long Id { get; set; }
    public Guid AccountId { get; set; }
    public string Action { get; set; } = "";
    public string Actor { get; set; } = "";
    public string Details { get; set; } = "";
    public DateTimeOffset OccurredUtc { get; set; }
}

public sealed class HandledStripeEvent
{
    public string Id { get; set; } = "";
    public DateTimeOffset HandledUtc { get; set; }
}

public sealed class AppDb(DbContextOptions<AppDb> options) : DbContext(options)
{
    public DbSet<Account> Accounts => Set<Account>();
    public DbSet<AuditEntry> Audit => Set<AuditEntry>();
    public DbSet<HandledStripeEvent> HandledStripeEvents => Set<HandledStripeEvent>();
    public DbSet<HistoryDeletionIntent> HistoryDeletionIntents => Set<HistoryDeletionIntent>();

    protected override void OnModelCreating(ModelBuilder model)
    {
        model.Entity<Account>().HasKey(x => x.Id);
        model.Entity<Account>().HasIndex(x => x.Email).IsUnique();
        model.Entity<Account>().HasIndex(x => x.StripeSubscriptionId).IsUnique();
        model.Entity<AuditEntry>().HasKey(x => x.Id);
        model.Entity<HandledStripeEvent>().HasKey(x => x.Id);
        model.Entity<HistoryDeletionIntent>().HasKey(x => x.Id);
        model.Entity<HistoryDeletionIntent>().HasIndex(x => new { x.AccountId, x.OpaquePath }).IsUnique();
    }
}

public record RegisterRequest(string Email, string Password, string Plan);
public record ResendVerificationRequest(string Email);
public record LoginRequest(string Email, string Password);
public record ExtendTrialRequest(int Days, string? Reason);
public record TemporaryQuotaRequest(int ExtraGiB, int Days, string? Reason);
public record CheckoutRequest(string Plan, string BillingInterval = "month");
public record SessionUrl(string Url);

public record AccountView(
    Guid Id, string Email, string Plan, long QuotaBytes,
    DateTimeOffset TrialUntilUtc, DateTimeOffset? PaidUntilUtc,
    string PaymentStatus, bool CanWrite, string Entitlement, bool EmailVerified,
    long BaseQuotaBytes, long GiftQuotaBytes, DateTimeOffset? GiftQuotaExpiresUtc);
public record AuthResponse(string AccessToken, AccountView Account);

public sealed record PlanTier(string Id, string Name, long QuotaBytes, int MonthlyUsdCents,
    int AnnualUsdCents)
{
    public int PriceCents(string interval) => interval switch
    {
        "month" => MonthlyUsdCents,
        "year" => AnnualUsdCents,
        _ => throw new ArgumentException("Unknown billing interval.")
    };
}

public static class PlanCatalog
{
    // Prices are public fixed-price proposals, VAT treatment varies by jurisdiction.
    // Stripe must return exactly these values in USD; never trust a client-supplied amount.
    public static readonly IReadOnlyDictionary<string, PlanTier> Tiers =
        new Dictionary<string, PlanTier>
        {
            ["basic"] = new("basic", "Mini", 10L * 1024 * 1024 * 1024, 199, 1788),
            ["plus"] = new("plus", "Plus", 75L * 1024 * 1024 * 1024, 599, 5988),
            ["pro"] = new("pro", "Pro", 500L * 1024 * 1024 * 1024, 2499, 23988)
        };

    public static readonly IReadOnlyDictionary<string, long> Quotas =
        Tiers.ToDictionary(p => p.Key, p => p.Value.QuotaBytes);
    public static bool IsValid(string? plan) => plan is not null && Tiers.ContainsKey(plan);
    public static bool ValidInterval(string? interval) => interval is "month" or "year";
    public const long GiB = 1024L * 1024 * 1024;
    public static long BaseQuotaBytes(Account a) => a.QuotaOverrideBytes ?? Quotas[a.Plan];
    public static long GiftQuotaBytes(Account a, DateTimeOffset now) =>
        a.GiftQuotaExpiresUtc > now && a.GiftQuotaGiB > 0 ? checked(a.GiftQuotaGiB * GiB) : 0;
    public static long EffectiveQuotaBytes(Account a, DateTimeOffset now) =>
        checked(BaseQuotaBytes(a) + GiftQuotaBytes(a, now));
    public static int EffectiveQuotaGiB(Account a, DateTimeOffset now) =>
        checked((int)((EffectiveQuotaBytes(a, now) + GiB - 1) / GiB));

    public static AccountView View(Account a, DateTimeOffset now)
    {
        bool paid = a.PaidUntilUtc is { } until && until > now &&
                    a.StripeStatus is "active" or "canceled";
        bool trial = a.TrialUntilUtc > now;
        bool verified = a.EmailVerifiedUtc is not null;
        string state = !verified ? "email_unverified" : paid ? "paid" : trial ? "trial" : "expired";
        return new(a.Id, a.Email, a.Plan, EffectiveQuotaBytes(a, now),
            a.TrialUntilUtc, a.PaidUntilUtc, a.StripeStatus, verified && (trial || paid), state, verified,
            BaseQuotaBytes(a), GiftQuotaBytes(a, now), a.GiftQuotaExpiresUtc > now ? a.GiftQuotaExpiresUtc : null);
    }
}

public static class TrialPolicy
{
    public static DateTimeOffset NewTrialEnd(DateTimeOffset now, bool isDevelopment, bool explicitlyEnabled)
        => isDevelopment && explicitlyEnabled ? now.AddYears(100) : now.AddMonths(1);

    public static DateTimeOffset Extend(DateTimeOffset currentEnd, DateTimeOffset now, int days)
    {
        if (days is < 1 or > 36500)
            throw new ArgumentOutOfRangeException(nameof(days), "Days must be between 1 and 36500.");
        return (currentEnd > now ? currentEnd : now).AddDays(days);
    }
}

public static class TemporaryQuotaPolicy
{
    public const int MaxGiftGiB = 1024;
    public const int MaxDays = 30;
    public static void Grant(Account account, int extraGiB, int days, DateTimeOffset now)
    {
        if (extraGiB is < 1 or > MaxGiftGiB || days is < 1 or > MaxDays)
            throw new ArgumentOutOfRangeException(nameof(extraGiB), "Gift quota must be 1..1024 GiB and last 1..30 days.");
        if (PlanCatalog.BaseQuotaBytes(account) + extraGiB * PlanCatalog.GiB > 10240L * PlanCatalog.GiB)
            throw new ArgumentOutOfRangeException(nameof(extraGiB), "Maximum total quota is 10 TiB.");
        account.GiftQuotaGiB = extraGiB; // Replace previous grant, never silently stack.
        account.GiftQuotaExpiresUtc = now.AddDays(days);
    }
    public static void Revoke(Account account)
    {
        account.GiftQuotaGiB = 0;
        account.GiftQuotaExpiresUtc = null;
    }
}
