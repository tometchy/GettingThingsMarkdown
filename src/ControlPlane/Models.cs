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

    protected override void OnModelCreating(ModelBuilder model)
    {
        model.Entity<Account>().HasKey(x => x.Id);
        model.Entity<Account>().HasIndex(x => x.Email).IsUnique();
        model.Entity<Account>().HasIndex(x => x.StripeSubscriptionId).IsUnique();
        model.Entity<AuditEntry>().HasKey(x => x.Id);
        model.Entity<HandledStripeEvent>().HasKey(x => x.Id);
    }
}

public record RegisterRequest(string Email, string Password, string Plan);
public record ResendVerificationRequest(string Email);
public record LoginRequest(string Email, string Password);
public record ExtendTrialRequest(int Days, string? Reason);
public record CheckoutRequest(string Plan, string BillingInterval = "month");
public record SessionUrl(string Url);

public record AccountView(
    Guid Id, string Email, string Plan, long QuotaBytes,
    DateTimeOffset TrialUntilUtc, DateTimeOffset? PaidUntilUtc,
    string PaymentStatus, bool CanWrite, string Entitlement, bool EmailVerified);
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

    public static AccountView View(Account a, DateTimeOffset now)
    {
        bool paid = a.PaidUntilUtc is { } until && until > now &&
                    a.StripeStatus is "active" or "canceled";
        bool trial = a.TrialUntilUtc > now;
        bool verified = a.EmailVerifiedUtc is not null;
        string state = !verified ? "email_unverified" : paid ? "paid" : trial ? "trial" : "expired";
        return new(a.Id, a.Email, a.Plan, a.QuotaOverrideBytes ?? Quotas[a.Plan],
            a.TrialUntilUtc, a.PaidUntilUtc, a.StripeStatus, verified && (trial || paid), state, verified);
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
