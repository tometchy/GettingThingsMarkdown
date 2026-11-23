using System.Buffers;
using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography.X509Certificates;
using SmtpServer;
using SmtpServer.ComponentModel;
using SmtpServer.Mail;
using SmtpServer.Protocol;
using SmtpServer.Storage;

namespace GettingThingsMarkdown.MailIngress;

// A separate shared SMTP service, not an extra per-tenant container. It keeps
// MIME in bounded memory until the control plane has durably stored encrypted
// content. NO plaintext files, mail queues, or email addresses in logs.
internal static class Program
{
    public static async Task Main()
    {
        string serverName = Required("GTM_MAIL_DOMAIN");
        string apiUrl = Required("GTM_MAIL_API_URL");
        string token = Required("GTM_MAIL_INGRESS_TOKEN");
        if (token.Length < 32) throw new InvalidOperationException("Ingress token too short.");
        string certPath = Required("GTM_SMTP_TLS_CERT");
        string keyPath = Required("GTM_SMTP_TLS_KEY");
        using var certificate = X509Certificate2.CreateFromPemFile(certPath, keyPath);
        using var api = new HttpClient {
            BaseAddress = new Uri(apiUrl, UriKind.Absolute),
            Timeout = TimeSpan.FromSeconds(20)
        };
        api.DefaultRequestHeaders.Add("X-GTM-Mail-Token", token);
        int port = int.TryParse(Environment.GetEnvironmentVariable("GTM_SMTP_PORT"),
            out int configured) ? configured : 2525;
        if (port is < 1 or > 65535) throw new ArgumentOutOfRangeException(nameof(port));
        var options = new SmtpServerOptionsBuilder()
            .ServerName(serverName)
            .Endpoint(b => b.Port(port).AllowUnsecureAuthentication(false).Certificate(certificate))
            .MaxMessageSize(256 * 1024)
            .CommandWaitTimeout(TimeSpan.FromSeconds(30))
            .Build();
        var services = new ServiceProvider();
        services.Add(new IngressMailStore(api));
        services.Add(new IngressMailboxFilter(api, serverName));
        var server = new global::SmtpServer.SmtpServer(options, services);
        Console.WriteLine("SMTP encrypted inbound gateway ready (no plaintext spool).");
        await server.StartAsync(CancellationToken.None);
    }

    private static string Required(string key) =>
        Environment.GetEnvironmentVariable(key) is { Length: > 0 } value
            ? value : throw new InvalidOperationException(key + " is required.");
}

internal sealed class IngressMailboxFilter(HttpClient api, string domain) : IMailboxFilter
{
    public Task<bool> CanAcceptFromAsync(ISessionContext context, IMailbox from,
        int size, CancellationToken ct) =>
        Task.FromResult(size < 0 || size <= 256 * 1024);

    public async Task<bool> CanDeliverToAsync(ISessionContext context, IMailbox to,
        IMailbox from, CancellationToken ct)
    {
        if (!to.Host.Equals(domain, StringComparison.OrdinalIgnoreCase) ||
            to.User.Length != 49 || to.User[0] != 'r' ||
            to.User.AsSpan(1).IndexOfAnyExcept("0123456789abcdef") != -1)
            return false;
        try
        {
            using var response = await api.GetAsync("internal/mail/recipient?recipient=" +
                Uri.EscapeDataString(to.User + "@" + to.Host), ct);
            if (response.IsSuccessStatusCode) return true;
            if (response.StatusCode == HttpStatusCode.NotFound) return false;
            // Transient outage must never turn into permanent SMTP 550.
            throw new SmtpResponseException(new SmtpResponse(SmtpReplyCode.Aborted,
                "Temporary recipient validation failure"));
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            throw new SmtpResponseException(new SmtpResponse(SmtpReplyCode.Aborted,
                "Recipient validation temporarily unavailable"));
        }
    }
}

internal sealed class IngressMailStore(HttpClient api) : MessageStore
{
    public override async Task<SmtpResponse> SaveAsync(ISessionContext context,
        IMessageTransaction transaction, ReadOnlySequence<byte> buffer,
        CancellationToken cancellationToken)
    {
        if (transaction.To.Count != 1) return SmtpResponse.NoValidRecipientsGiven;
        if (buffer.Length is <= 0 or > 256 * 1024) return SmtpResponse.SizeLimitExceeded;
        string to = transaction.To[0].User + "@" + transaction.To[0].Host;
        using var request = new HttpRequestMessage(HttpMethod.Post,
            "internal/mail/receive?recipient=" + Uri.EscapeDataString(to));
        request.Content = new ByteArrayContent(buffer.ToArray());
        request.Content.Headers.ContentType =
            new MediaTypeHeaderValue("message/rfc822");
        try
        {
            using var response = await api.SendAsync(request, cancellationToken);
            if (response.IsSuccessStatusCode) return SmtpResponse.Ok;
            if (response.StatusCode is HttpStatusCode.NotFound or
                HttpStatusCode.Forbidden or HttpStatusCode.BadRequest)
                return SmtpResponse.MailboxUnavailable;
            if (response.StatusCode == HttpStatusCode.RequestEntityTooLarge)
                return SmtpResponse.SizeLimitExceeded;
            // Do not confirm SMTP DATA unless encrypted bytes are committed.
            // 4xx causes senders to retry on temporary storage/rate failures.
            return new SmtpResponse(SmtpReplyCode.Aborted,
                "Temporary encrypted inbox delivery failure");
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            return new SmtpResponse(SmtpReplyCode.Aborted,
                "Encrypted inbox temporarily unavailable");
        }
    }
}
