using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using MimeKit;

namespace GettingThingsMarkdown.ControlPlane;

/// <summary>
/// Incoming SMTP is necessarily plaintext until this process encrypts it.
/// Neither the private key nor the Syncthing folder encryption secret is known
/// to the SMTP gateway or control plane. The exact original MIME is preserved inside the encrypted payload.
/// </summary>
public static class MailEncryption
{
    private static readonly byte[] DomainSeparator = "GTM-INBOX-V1"u8.ToArray();

    public static bool ValidatePublicKey(string key)
    {
        try
        {
            var bytes = Convert.FromBase64String(key);
            if (bytes.Length is < 390 or > 650) return false;
            using var rsa = RSA.Create();
            rsa.ImportSubjectPublicKeyInfo(bytes, out int consumed);
            return consumed == bytes.Length && rsa.KeySize is >= 3072 and <= 4096;
        }
        catch (Exception ex) when (ex is FormatException or CryptographicException)
        {
            return false;
        }
    }

    public static async Task<EncryptedInboundMail> EncryptAsync(
        Guid accountId, string key, byte[] rawMessage, DateTimeOffset now,
        CancellationToken ct)
    {
        if (MailKeySet.Read(key).Length is < 1 or > MailKeySet.MaximumKeys ||
            !MailKeySet.Read(key).All(ValidatePublicKey) || rawMessage.Length is 0 or > MailInboxPolicy.MaxRawMailBytes)
            throw new ArgumentException("Invalid key or message size.");
        await using var input = new MemoryStream(rawMessage, writable: false);
        var email = await MimeMessage.LoadAsync(input, ct);
        // TextBody handles plain-text alternatives, quoted-printable and UTF-8
        // without accepting HTML or attachment contents into tasks.
        var text = email.TextBody ??
            (email.HtmlBody is { } html
                ? new MimeKit.Text.HtmlToText { OutputLinkUrls = true,
                    OutputImageAltText = true }.Convert(html)
                : "[Message has no text body]");
        // No HTML is stored or executed. Verification URLs remain as plain text.

        var normalized = new InboundMailPayload(
            Limit(email.Subject ?? "(no subject)", 400),
            Limit(email.From.ToString(), 254),
            Limit(text, 32768), now, Convert.ToBase64String(rawMessage));
        var plaintext = JsonSerializer.SerializeToUtf8Bytes(normalized,
            new JsonSerializerOptions(JsonSerializerDefaults.Web) {
                Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping });
        var keyMaterial = RandomNumberGenerator.GetBytes(32);
        var nonce = RandomNumberGenerator.GetBytes(12);
        var encrypted = new byte[plaintext.Length];
        var tag = new byte[16];
        byte[] wrapped;
        try
        {
            wrapped = MailKeySet.Wrap(key, keyMaterial);
            using var aes = new AesGcm(keyMaterial, 16);
            aes.Encrypt(nonce, plaintext, encrypted, tag, DomainSeparator);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(keyMaterial);
            CryptographicOperations.ZeroMemory(plaintext);
        }
        return new EncryptedInboundMail
        {
            AccountId = accountId,
            WrappedKey = wrapped,
            Nonce = nonce,
            Tag = tag,
            Ciphertext = encrypted,
            DedupSha256 = Convert.ToHexString(SHA256.HashData(rawMessage)),
            ReceivedUtc = now
        };
    }

    public static InboundMailPayload DecryptForTestOnly(EncryptedInboundMail message, RSA privateKey)
    {
        var aesKey = MailKeySet.Unwrap(message.WrappedKey, privateKey);
        var plaintext = new byte[message.Ciphertext.Length];
        try
        {
            using var aes = new AesGcm(aesKey, 16);
            aes.Decrypt(message.Nonce, message.Ciphertext, message.Tag, plaintext, DomainSeparator);
            return JsonSerializer.Deserialize<InboundMailPayload>(plaintext,
                new JsonSerializerOptions(JsonSerializerDefaults.Web))!;
        }
        finally
        {
            CryptographicOperations.ZeroMemory(aesKey);
            CryptographicOperations.ZeroMemory(plaintext);
        }
    }

    public static MailEnvelope View(EncryptedInboundMail x) => new(
        x.Id, Convert.ToBase64String(x.WrappedKey), Convert.ToBase64String(x.Nonce),
        Convert.ToBase64String(x.Tag), Convert.ToBase64String(x.Ciphertext), x.ReceivedUtc);

    private static string Limit(string s, int length) =>
        s.Length > length ? s[..length] : s;
}
