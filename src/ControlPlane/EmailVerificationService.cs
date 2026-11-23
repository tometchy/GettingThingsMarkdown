using System.Net;
using System.Net.Mail;
using System.Security.Cryptography;
using System.Text;

namespace GettingThingsMarkdown.ControlPlane;

/// <summary>Email validation is required before any paid cloud tenant is
/// provisioned. All token material is random and stored only as SHA256.</summary>
public sealed class EmailVerificationService(IConfiguration config)
{
    public static string NewToken() => Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
    public static string Hash(string token) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(token)));

    public static bool Matches(string token, string storedHash)
    {
        if (token.Length > 256) return false;
        try
        {
            byte[] actual = Convert.FromHexString(storedHash);
            var candidate = SHA256.HashData(Encoding.UTF8.GetBytes(token));
            return CryptographicOperations.FixedTimeEquals(candidate, actual);
        }
        catch (FormatException) { return false; }
    }

    public static bool IsConfigured(IConfiguration config) =>
        !string.IsNullOrWhiteSpace(config["GTM_SMTP_HOST"]) &&
        !string.IsNullOrWhiteSpace(config["GTM_SMTP_USERNAME"]) &&
        !string.IsNullOrWhiteSpace(config["GTM_SMTP_PASSWORD"]) &&
        !string.IsNullOrWhiteSpace(config["GTM_SMTP_FROM"]) &&
        Uri.TryCreate(config["GTM_PUBLIC_URL"], UriKind.Absolute, out var uri) &&
        uri.Scheme == Uri.UriSchemeHttps;

    public async Task SendAsync(string address, string token, CancellationToken ct)
    {
        if (!IsConfigured(config))
            throw new InvalidOperationException("SMTP not configured.");
        var url = config["GTM_PUBLIC_URL"]!.TrimEnd('/') +
            "/api/auth/verify-email?email=" + Uri.EscapeDataString(address) +
            "&token=" + Uri.EscapeDataString(token);
        using var message = new MailMessage(config["GTM_SMTP_FROM"]!, address)
        {
            Subject = "GettingThingsMarkdown — potwierdź adres e-mail / verify email",
            Body = "Otwórz link, aby aktywować miesięczny trial:\n" + url +
                   "\n\nOpen this link to activate your one-month free trial:\n" + url +
                   "\n\nLink wygasa po 24 godzinach / valid for 24 hours."
        };
        using var client = new SmtpClient(config["GTM_SMTP_HOST"]!,
            int.TryParse(config["GTM_SMTP_PORT"], out int port) ? port : 587)
        {
            EnableSsl = true, UseDefaultCredentials = false,
            Credentials = new NetworkCredential(config["GTM_SMTP_USERNAME"],
                config["GTM_SMTP_PASSWORD"]),
            Timeout = 12000
        };
        await client.SendMailAsync(message, ct);
    }
}
