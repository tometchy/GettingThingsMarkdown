using GettingThingsMarkdown.ControlPlane;

namespace GettingThingsMarkdown.Tests;

public class TrialPolicyTests
{
    private static readonly DateTimeOffset Now = new(2026, 11, 22, 0, 0, 0, TimeSpan.Zero);

    [Fact]
    public void OrdinaryTrialIsOneCalendarMonth()
    {
        Assert.Equal(Now.AddMonths(1), TrialPolicy.NewTrialEnd(Now, false, false));
        Assert.Equal(Now.AddMonths(1), TrialPolicy.NewTrialEnd(Now, true, false));
    }

    [Fact]
    public void ExplicitDevTrialIs100YearsButNeverInProduction()
    {
        Assert.Equal(Now.AddYears(100), TrialPolicy.NewTrialEnd(Now, true, true));
        Assert.Equal(Now.AddMonths(1), TrialPolicy.NewTrialEnd(Now, false, true));
    }

    [Fact]
    public void AdminExtendsFromExistingExpiryOrNow()
    {
        Assert.Equal(Now.AddDays(40), TrialPolicy.Extend(Now.AddDays(10), Now, 30));
        Assert.Equal(Now.AddDays(30), TrialPolicy.Extend(Now.AddDays(-2), Now, 30));
        Assert.Throws<ArgumentOutOfRangeException>(() => TrialPolicy.Extend(Now, Now, 0));
    }

    [Fact]
    public void EntitlementExpiresServerSide()
    {
        var a = new Account { Plan = "plus", TrialUntilUtc = Now.AddDays(1), EmailVerifiedUtc = Now };
        Assert.Equal("trial", PlanCatalog.View(a, Now).Entitlement);
        Assert.Equal("expired", PlanCatalog.View(a, Now.AddDays(2)).Entitlement);
        Assert.Equal(75L * 1024 * 1024 * 1024, PlanCatalog.View(a, Now).QuotaBytes);
    }
}
