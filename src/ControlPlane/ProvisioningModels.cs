using Microsoft.EntityFrameworkCore;

namespace GettingThingsMarkdown.ControlPlane;

public sealed class TenantState
{
    public Guid AccountId { get; set; }
    public string Namespace { get; set; } = "";
    public string Status { get; set; } = "Pending";
    public string? ServerDeviceId { get; set; }
    public DateTimeOffset UpdatedUtc { get; set; }
    public string? LastError { get; set; }
}

public sealed class ProvisioningDb(DbContextOptions<ProvisioningDb> options) : DbContext(options)
{
    public DbSet<TenantState> Tenants => Set<TenantState>();
    protected override void OnModelCreating(ModelBuilder builder)
    {
        builder.Entity<TenantState>().HasKey(x => x.AccountId);
        builder.Entity<TenantState>().HasIndex(x => x.Namespace).IsUnique();
    }
}

public record TenantStatusView(string Status, string? ServerDeviceId, string? Detail,
    string FolderId, string? LocalSyncAddress);

public record AddDeviceRequest(string DeviceId, string? Name);

public static class TenantNames
{
    public static string Namespace(Guid accountId) => "gtm-" + accountId.ToString("N");
    public static string FolderId => "notes";
}
