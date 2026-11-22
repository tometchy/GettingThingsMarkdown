using GettingThingsMarkdown.ControlPlane;

namespace GettingThingsMarkdown.Tests;

public sealed class GitHistoryProxyTests
{
    [Theory]
    [InlineData("0123456789abcdef0123456789abcdef01234567", true)]
    [InlineData("0123456789abcdef0123456789abcdef01234567/../secret", false)]
    [InlineData("ABCDEF0123456789ABCDEF0123456789ABCDEF01", false)]
    [InlineData("abc", false)]
    [InlineData("", false)]
    public void OnlyLowercaseGitOidsAllowed(string oid, bool expected)
        => Assert.Equal(expected, GitHistoryProxy.ValidGitOid(oid));
}
