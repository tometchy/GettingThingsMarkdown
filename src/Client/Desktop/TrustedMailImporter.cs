using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using GettingThingsMarkdown.Automations;
using GettingThingsMarkdown.Client.Shared;

namespace GettingThingsMarkdown.Client.Desktop;

/// <summary>
/// Asymmetric mailbox private keys never leave a trusted device. The SMTP
/// server only sees the corresponding RSA SPKI public key. This importer turns
/// encrypted envelopes into ordinary local .md files; Syncthing then performs
/// its normal untrusted-device E2EE upload.
/// </summary>
public static class TrustedMailImporter
{
    private static readonly byte[] DomainSeparator = "GTM-INBOX-V1"u8.ToArray();

    private static string KeyPath(string keyDir, Guid owner) =>
        Path.Combine(keyDir, "mail-" + owner.ToString("N") + ".pem");

    public static async Task<string> EnsurePublicKeyAsync(
        string keyDir, Guid accountId, CancellationToken ct = default)
    {
        var path = KeyPath(keyDir, accountId);
        if (!File.Exists(path))
        {
            Directory.CreateDirectory(keyDir);
            using var newKey = RSA.Create(3072);
            try
            {
                await using var file = new FileStream(path, FileMode.CreateNew,
                    FileAccess.Write, FileShare.None, 4096, FileOptions.Asynchronous);
                await using var writer = new StreamWriter(file, new UTF8Encoding(false));
                await writer.WriteAsync(newKey.ExportPkcs8PrivateKeyPem().AsMemory(), ct);
            }
            catch (IOException) when (File.Exists(path))
            {
                // Another trusted instance created the key first; never overwrite it.
            }
            RestrictPermissions(path);
        }
        using var existing = RSA.Create();
        existing.ImportFromPem(await File.ReadAllTextAsync(path, ct));
        if (existing.KeySize is < 3072 or > 4096)
            throw new CryptographicException("Unexpected email private key size.");
        return Convert.ToBase64String(existing.ExportSubjectPublicKeyInfo());
    }

    public static Task<string?> ExportPrivateKeyAsync(string keyDir, Guid accountId,
        CancellationToken ct = default)
    {
        var path = KeyPath(keyDir, accountId);
        return File.Exists(path) ? File.ReadAllTextAsync(path, ct).ContinueWith<string?>(
            t => t.GetAwaiter().GetResult(), ct) : Task.FromResult<string?>(null);
    }

    public static async Task ImportPrivateKeyAsync(string keyDir, Guid accountId,
        string privateKeyPem, CancellationToken ct = default)
    {
        using var rsa = RSA.Create();
        rsa.ImportFromPem(privateKeyPem);
        if (rsa.KeySize is < 3072 or > 4096)
            throw new CryptographicException("RSA private key must be >=3072 bits.");
        // Force public/private key pair validation before saving.
        _ = rsa.ExportPkcs8PrivateKey();
        Directory.CreateDirectory(keyDir);
        var path = KeyPath(keyDir, accountId);
        if (File.Exists(path))
        {
            using var oldKey = RSA.Create();
            oldKey.ImportFromPem(await File.ReadAllTextAsync(path, ct));
            if (!oldKey.ExportSubjectPublicKeyInfo().AsSpan()
                    .SequenceEqual(rsa.ExportSubjectPublicKeyInfo()))
                throw new InvalidOperationException(
                    "This device already has a different email key. Refusing replacement.");
            return;
        }
        await using var stream = new FileStream(path, FileMode.CreateNew,
            FileAccess.Write, FileShare.None, 4096, FileOptions.Asynchronous);
        await using (var writer = new StreamWriter(stream, new UTF8Encoding(false), leaveOpen: true))
            await writer.WriteAsync(rsa.ExportPkcs8PrivateKeyPem().AsMemory(), ct);
        RestrictPermissions(path);
    }

    public static async Task<int> ImportAsync(string keyDir, Guid accountId, AccountApi api,
        string workspaceRoot, CancellationToken ct = default)
    {
        var key = KeyPath(keyDir, accountId);
        if (!File.Exists(key))
            throw new InvalidOperationException(
                "Inbox private key missing. Import from an existing trusted device.");
        using var privateKey = RSA.Create();
        privateKey.ImportFromPem(await File.ReadAllTextAsync(key, ct));
        var workspace = new MarkdownWorkspace(workspaceRoot);
        int imported = 0;
        foreach (var mail in await api.MailMessagesAsync(ct))
        {
            ct.ThrowIfCancellationRequested();
            string relative = MailTaskFormatter.FileName(mail.Id);
            var path = workspace.Resolve(relative);
            // If HTTP ACK previously failed after atomic write, retry without
            // overwriting the user's edits to an imported task.
            if (!File.Exists(path))
            {
                var message = Decrypt(mail, privateKey);
                await workspace.SaveAsync(relative, MailTaskFormatter.Render(
                    message.Subject, message.From, message.Body, message.ReceivedUtc), ct);
                imported++;
            }
            // Never ACK before the local atomic write has succeeded.
            await api.AckMailAsync(mail.Id, ct);
        }
        return imported;
    }

    public static MailMessagePayload Decrypt(MailEnvelopeInfo mail, RSA privateKey)
    {
        var wrapped = Convert.FromBase64String(mail.WrappedKey);
        var iv = Convert.FromBase64String(mail.Nonce);
        var tag = Convert.FromBase64String(mail.Tag);
        var ciphertext = Convert.FromBase64String(mail.Ciphertext);
        if (iv.Length != 12 || tag.Length != 16 || ciphertext.Length > 128 * 1024)
            throw new CryptographicException("Invalid encrypted mail envelope.");
        var aesKey = privateKey.Decrypt(wrapped, RSAEncryptionPadding.OaepSHA256);
        var plain = new byte[ciphertext.Length];
        try
        {
            using var aes = new AesGcm(aesKey, 16);
            aes.Decrypt(iv, ciphertext, tag, plain, DomainSeparator);
            return JsonSerializer.Deserialize(plain,
                AccountJsonContext.Default.MailMessagePayload)
                ?? throw new CryptographicException("Invalid decrypted mail.");
        }
        finally
        {
            CryptographicOperations.ZeroMemory(plain);
            CryptographicOperations.ZeroMemory(aesKey);
        }
    }

    private static void RestrictPermissions(string path)
    {
        if (OperatingSystem.IsLinux() || OperatingSystem.IsMacOS())
            File.SetUnixFileMode(path,
                UnixFileMode.UserRead | UnixFileMode.UserWrite);
    }
}
