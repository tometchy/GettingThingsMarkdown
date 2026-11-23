using GettingThingsMarkdown.ControlPlane;

namespace GettingThingsMarkdown.Tests;

public sealed class HistoryProtectionPolicyTests
{
    private static readonly DateTimeOffset Now =
        new(2026, 11, 23, 12, 0, 0, TimeSpan.Zero);

    [Theory]
    [InlineData("../secret", false)]
    [InlineData("/absolute", false)]
    [InlineData("dir/../secret", false)]
    [InlineData("dir//name", false)]
    [InlineData("dir\\name", false)]
    [InlineData("valid/ciphertext-AZ94", true)]
    [InlineData("encrypted-name", true)]
    public void OnlyCanonicalRelativeOpaquePathsAccepted(string path, bool expected)
        => Assert.Equal(expected, HistoryProtectionPolicy.ValidOpaquePath(path));

    [Fact]
    public void RequestRetainsHistorySevenDaysAndCanBeUndone()
    {
        var id = Guid.NewGuid();
        var intent = HistoryProtectionPolicy.New(id, "ciphertext/filename", Now);
        Assert.Equal(id, intent.AccountId);
        Assert.Equal(Now.AddDays(7), intent.PurgeNotBeforeUtc);
        Assert.Equal("Pending", intent.State);
        HistoryProtectionPolicy.Cancel(intent, Now.AddDays(5));
        Assert.Equal("Canceled", intent.State);
        Assert.Throws<InvalidOperationException>(() =>
            HistoryProtectionPolicy.Cancel(intent, Now.AddDays(6)));
        HistoryProtectionPolicy.RestartCanceled(intent, Now.AddDays(10));
        Assert.Equal(Now.AddDays(17), intent.PurgeNotBeforeUtc);
        Assert.Null(intent.CanceledUtc);
    }

    [Fact]
    public void HoldAppliesAcrossTenantAndCannotBeShortened()
    {
        var account = new Account();
        var intent = HistoryProtectionPolicy.New(account.Id, "opaque", Now);
        Assert.Equal(Now.AddDays(7), HistoryProtectionPolicy.EffectiveNotBefore(intent, account));
        Assert.Equal(Now.AddDays(30),
            HistoryProtectionPolicy.ExtendTenantHold(account, Now, 30));
        Assert.Equal(Now.AddDays(30), HistoryProtectionPolicy.EffectiveNotBefore(intent, account));
        Assert.Equal(Now.AddDays(30),
            HistoryProtectionPolicy.ExtendTenantHold(account, Now.AddDays(1), 2));
        Assert.Equal(Now.AddDays(31),
            HistoryProtectionPolicy.ExtendTenantHold(account, Now.AddDays(1), 30));
        Assert.Throws<ArgumentOutOfRangeException>(() =>
            HistoryProtectionPolicy.ExtendTenantHold(account, Now, 366));
    }

    [Fact]
    public void NoPurgingIsImpliedByAnExpiredDeadline()
    {
        var account = new Account();
        var intent = HistoryProtectionPolicy.New(account.Id, "opaque", Now.AddDays(-20));
        Assert.Equal("Pending", intent.State);
        // The future purge worker is deliberately not implemented. A deadline
        // does NOT itself perform GC or authorize destruction of either replica.
        Assert.True(HistoryProtectionPolicy.EffectiveNotBefore(intent, account) < Now);
    }
}
