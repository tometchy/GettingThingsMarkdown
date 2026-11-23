using GettingThingsMarkdown.ControlPlane;

namespace GettingThingsMarkdown.Tests;

public class TemporaryQuotaTests
{
    private static Account NewAccount() => new() { Plan = "basic", TrialUntilUtc = DateTimeOffset.UtcNow.AddDays(30), EmailVerifiedUtc = DateTimeOffset.UtcNow };

    [Fact]
    public void GrantAddsQuotaWithoutChangingPlanAndExpiresPrecisely()
    {
        var now = DateTimeOffset.UtcNow;
        var account = NewAccount();
        TemporaryQuotaPolicy.Grant(account, 1, 7, now);
        Assert.Equal("basic", account.Plan);
        Assert.Equal(11 * PlanCatalog.GiB, PlanCatalog.EffectiveQuotaBytes(account, now));
        Assert.Equal(10 * PlanCatalog.GiB, PlanCatalog.EffectiveQuotaBytes(account, now.AddDays(7)));
        Assert.Null(PlanCatalog.View(account, now.AddDays(7)).GiftQuotaExpiresUtc);
        TemporaryQuotaPolicy.Grant(account, 2, 1, now.AddDays(1));
        Assert.Equal(12 * PlanCatalog.GiB, PlanCatalog.EffectiveQuotaBytes(account, now.AddDays(1)));
        TemporaryQuotaPolicy.Revoke(account);
        Assert.Equal(10 * PlanCatalog.GiB, PlanCatalog.EffectiveQuotaBytes(account, now));
    }

    [Theory]
    [InlineData(0, 7)]
    [InlineData(1025, 7)]
    [InlineData(1, 0)]
    [InlineData(1, 31)]
    public void RejectsAbusiveGrants(int gb, int days)
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => TemporaryQuotaPolicy.Grant(NewAccount(), gb, days, DateTimeOffset.UtcNow));
    }
}
