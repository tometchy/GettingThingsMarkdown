using GettingThingsMarkdown.ControlPlane;

namespace GettingThingsMarkdown.Tests;

public sealed class EmailVerificationTests
{
    [Fact]
    public void TokenIsRandomHashedAndTimeSafe()
    {
        var token = EmailVerificationService.NewToken();
        var other = EmailVerificationService.NewToken();
        Assert.Equal(64, token.Length);
        Assert.NotEqual(token, other);
        Assert.True(EmailVerificationService.Matches(token,
            EmailVerificationService.Hash(token)));
        Assert.False(EmailVerificationService.Matches(other,
            EmailVerificationService.Hash(token)));
        Assert.False(EmailVerificationService.Matches("bad", "invalid!"));
    }

    [Fact]
    public void UnverifiedAccountsHaveNoWriteEntitlement()
    {
        var a = new Account { Plan = "basic",
            TrialUntilUtc = DateTimeOffset.UtcNow.AddDays(30) };
        var entitlement = PlanCatalog.View(a, DateTimeOffset.UtcNow);
        Assert.False(entitlement.CanWrite);
        Assert.Equal("email_unverified", entitlement.Entitlement);
    }
}
