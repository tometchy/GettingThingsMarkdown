using System.Security.Cryptography;
using System.Text;
using GettingThingsMarkdown.ControlPlane;

namespace GettingThingsMarkdown.Tests;

public sealed class MailEncryptionTests
{
    [Fact]
    public async Task SmtpMessageIsEncryptedWithPublicKeyAndDecryptableOnlyWithPrivateKey()
    {
        using var recipient = RSA.Create(3072);
        using var other = RSA.Create(3072);
        var publicKey = Convert.ToBase64String(recipient.ExportSubjectPublicKeyInfo());
        const string raw = "From: Sender <sender@example.com>\r\n" +
            "To: r123@inbox.example.org\r\nSubject: =?UTF-8?B?WmFkYW5pZSDDpMO2?=\r\n" +
            "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nDo the task.\r\n";
        var entry = await MailEncryption.EncryptAsync(Guid.NewGuid(), publicKey,
            Encoding.UTF8.GetBytes(raw), DateTimeOffset.UtcNow, CancellationToken.None);
        Assert.NotEmpty(entry.WrappedKey);
        Assert.NotEmpty(entry.Ciphertext);
        Assert.DoesNotContain("Do the task", Encoding.UTF8.GetString(entry.Ciphertext));
        Assert.Throws<CryptographicException>(() => MailEncryption.DecryptForTestOnly(entry, other));
        var restored = MailEncryption.DecryptForTestOnly(entry, recipient);
        Assert.Equal("Do the task.", restored.Body.Trim());
        Assert.Contains("sender@example.com", restored.From);
        Assert.NotEqual("", restored.Subject);
        entry.Ciphertext[0] ^= 1;
        Assert.Throws<AuthenticationTagMismatchException>(() =>
            MailEncryption.DecryptForTestOnly(entry, recipient));
    }

    [Theory]
    [InlineData("basic", false)]
    [InlineData("plus", true)]
    [InlineData("pro", true)]
    public void OnlyEligiblePlansCanUseMailInbox(string plan, bool eligible)
    {
        var account = new Account { Plan = plan, EmailVerifiedUtc = DateTimeOffset.UtcNow,
            TrialUntilUtc = DateTimeOffset.UtcNow.AddDays(7) };
        Assert.Equal(eligible, MailInboxPolicy.IsEligible(account, DateTimeOffset.UtcNow));
        account.TrialUntilUtc = DateTimeOffset.UtcNow.AddDays(-1);
        Assert.False(MailInboxPolicy.IsEligible(account, DateTimeOffset.UtcNow));
    }

    [Fact]
    public void RandomAliasesAreNotGuessableAndAreStrictlyScopedToConfiguredDomain()
    {
        var first = MailInboxPolicy.NewLocalPart();
        var second = MailInboxPolicy.NewLocalPart();
        Assert.NotEqual(first, second);
        Assert.True(MailInboxPolicy.ValidRecipient(first + "@mail.example.org",
            "mail.example.org", out var result));
        Assert.Equal(first, result);
        Assert.False(MailInboxPolicy.ValidRecipient(first + "@evil.example.org",
            "mail.example.org", out _));
        Assert.False(MailInboxPolicy.ValidRecipient("ordinary@mail.example.org",
            "mail.example.org", out _));
    }

    [Fact]
    public void PublicKeyValidationRequiresRsa3072OrStronger()
    {
        using var weak = RSA.Create(2048);
        using var strong = RSA.Create(3072);
        Assert.False(MailEncryption.ValidatePublicKey(
            Convert.ToBase64String(weak.ExportSubjectPublicKeyInfo())));
        Assert.True(MailEncryption.ValidatePublicKey(
            Convert.ToBase64String(strong.ExportSubjectPublicKeyInfo())));
    }
}
