using System.Security.Cryptography;
using System.Text;
using GettingThingsMarkdown.ControlPlane;
namespace GettingThingsMarkdown.Tests;
public sealed class MailKeySetTests
{
    [Fact]
    public async Task IndependentKeysDecryptExactOriginalMime()
    {
        using var a = RSA.Create(3072);
        using var b = RSA.Create(3072);
        string keys = MailKeySet.Append(Convert.ToBase64String(a.ExportSubjectPublicKeyInfo()),
            Convert.ToBase64String(b.ExportSubjectPublicKeyInfo()));
        byte[] raw = Encoding.UTF8.GetBytes("From: sender@example.com\r\nSubject: Attachment\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\nContent-Type: text/plain\r\n\r\nhello\r\n--x\r\nContent-Type: application/pdf; name=a.pdf\r\nContent-Disposition: attachment; filename=a.pdf\r\nContent-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQ=\r\n--x--\r\n");
        var envelope = await MailEncryption.EncryptAsync(Guid.NewGuid(), keys, raw, DateTimeOffset.UtcNow, default);
        foreach (var key in new[] { a, b })
            Assert.Equal(raw, Convert.FromBase64String(MailEncryption.DecryptForTestOnly(envelope, key).RawMimeBase64!));
    }
}
