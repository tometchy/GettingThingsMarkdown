using Microsoft.EntityFrameworkCore;

namespace GettingThingsMarkdown.ControlPlane;

public sealed class Account
{
    public Guid Id { get; set; } = Guid.NewGuid();
    public string Email { get; set; } = "";
    public string PasswordHash { get; set; } = "";
    public string Plan { get; set; } = "basic";
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
public record LoginRequest(string Email, string Password);
public record ExtendTrialRequest(int Days, string? Reason);
public record CheckoutRequest(string Plan);
public record SessionUrl(string Url);

public record AccountView(
    Guid Id, string Email, string Plan, long QuotaBytes,
    DateTimeOffset TrialUntilUtc, DateTimeOffset? PaidUntilUtc,
    string PaymentStatus, bool CanWrite, string Entitlement);
public record AuthResponse(string AccessToken, AccountView Account);

public static class PlanCatalog
{
    public static readonly IReadOnlyDictionary<string, long> Quotas = new Dictionary<string, long>
    {
        ["basic"] = 15L * 1024 * 1024 * 1024,
        ["plus"] = 200L * 1024 * 1024 * 1024,
        ["pro"] = 1024L * 1024 * 1024 * 1024
    };

    public static bool IsValid(string? plan) => plan is not null && Quotas.ContainsKey(plan);

    public static AccountView View(Account a, DateTimeOffset now)
    {
        // A verified, paid period outlives a local trial. Invoice reconciliation is server-owned.
        bool paid = a.PaidUntilUtc is { } until && until > now &&
                    a.StripeStatus is "active" or "canceled";
        bool trial = a.TrialUntilUtc > now;
        string state = paid ? "paid" : trial ? "trial" : "expired";
        return new(a.Id, a.Email, a.Plan, Quotas[a.Plan], a.TrialUntilUtc,
            a.PaidUntilUtc, a.StripeStatus, trial || paid, state);
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
