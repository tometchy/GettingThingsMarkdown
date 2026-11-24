using System.Security.Cryptography;
using System.Text.Json;
namespace GettingThingsMarkdown.ControlPlane;
public static class MailKeySet
{
    public const int MaximumKeys = 16;
    public static string[] Read(string stored) => stored.StartsWith("[", StringComparison.Ordinal)
        ? JsonSerializer.Deserialize<string[]>(stored) ?? [] : string.IsNullOrEmpty(stored) ? [] : [stored];
    public static string Append(string stored, string key)
    {
        var keys = Read(stored);
        if (keys.Contains(key, StringComparer.Ordinal)) return stored;
        if (keys.Length >= MaximumKeys) throw new InvalidOperationException("Mailbox device limit reached.");
        return JsonSerializer.Serialize(keys.Append(key).ToArray());
    }
    public static byte[] Wrap(string stored, byte[] key)
    {
        var keys = Read(stored);
        if (keys.Length is < 1 or > MaximumKeys) throw new CryptographicException("Invalid recipient set.");
        var wrapped = keys.Select(publicKey => {
            using var rsa = RSA.Create();
            rsa.ImportSubjectPublicKeyInfo(Convert.FromBase64String(publicKey), out _);
            return rsa.Encrypt(key, RSAEncryptionPadding.OaepSHA256);
        }).ToArray();
        return wrapped.Length == 1 ? wrapped[0] : JsonSerializer.SerializeToUtf8Bytes(wrapped);
    }
    public static byte[] Unwrap(byte[] wrapped, RSA privateKey)
    {
        if (wrapped.Length is 384 or 512)
            return privateKey.Decrypt(wrapped, RSAEncryptionPadding.OaepSHA256);
        foreach (var candidate in JsonSerializer.Deserialize<byte[][]>(wrapped) ?? [])
        {
            try { return privateKey.Decrypt(candidate, RSAEncryptionPadding.OaepSHA256); }
            catch (CryptographicException) { }
        }
        throw new CryptographicException("No encrypted content key for this device.");
    }
}
