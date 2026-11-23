using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using GettingThingsMarkdown.ControlPlane;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;

namespace GettingThingsMarkdown.Tests;

[Collection("Integration environment")]
public sealed class MailInboxApiTests
{
    [Fact]
    public async Task SmtpGatewayAcceptsOnlyPlusProAndQueuesDecryptableCiphertextUntilLocalAck()
    {
        var path = Path.Combine(Path.GetTempPath(), "gtm-mail-" + Guid.NewGuid() + ".sqlite");
        var states = Path.Combine(Path.GetTempPath(), "gtm-mail-state-" + Guid.NewGuid() + ".sqlite");
        string[] keys = ["GTM_DB", "GTM_PROVISIONING_DB", "GTM_JWT_SECRET",
            "GTM_INBOUND_MAIL_DOMAIN", "GTM_MAIL_INGRESS_TOKEN", "GTM_KUBE_ENABLED"];
        var previous = keys.ToDictionary(x => x, Environment.GetEnvironmentVariable);
        try
        {
            Environment.SetEnvironmentVariable("GTM_DB", "Data Source=" + path);
            Environment.SetEnvironmentVariable("GTM_PROVISIONING_DB", "Data Source=" + states);
            Environment.SetEnvironmentVariable("GTM_JWT_SECRET", "mail-api-tests-use-a-random-32-plus-character-secret");
            Environment.SetEnvironmentVariable("GTM_INBOUND_MAIL_DOMAIN", "inbox.example.test");
            Environment.SetEnvironmentVariable("GTM_MAIL_INGRESS_TOKEN",
                "inbound-mail-only-token-more-than-32-characters");
            Environment.SetEnvironmentVariable("GTM_KUBE_ENABLED", "false");

            using var factory = new WebApplicationFactory<Program>()
                .WithWebHostBuilder(h => h.UseEnvironment("Development"));
            using var mini = factory.CreateClient();
            using var plus = factory.CreateClient();
            using var gateway = factory.CreateClient();
            var first = await mini.PostAsJsonAsync("/api/auth/register", new {
                email = "mini-mail@example.test", password = "mini-account-password-long",
                plan = "basic" });
            var miniAuth = await first.Content.ReadFromJsonAsync<AuthResponse>();
            mini.DefaultRequestHeaders.Authorization =
                new AuthenticationHeaderValue("Bearer", miniAuth!.AccessToken);
            Assert.Equal(HttpStatusCode.Forbidden,
                (await mini.GetAsync("/api/mail/inbox")).StatusCode);

            var registered = await plus.PostAsJsonAsync("/api/auth/register", new {
                email = "plus-mail@example.test", password = "plus-account-password-long",
                plan = "plus" });
            Assert.Equal(HttpStatusCode.Created, registered.StatusCode);
            var plusAuth = await registered.Content.ReadFromJsonAsync<AuthResponse>();
            plus.DefaultRequestHeaders.Authorization =
                new AuthenticationHeaderValue("Bearer", plusAuth!.AccessToken);

            using var rsa = RSA.Create(3072);
            var publicKey = Convert.ToBase64String(rsa.ExportSubjectPublicKeyInfo());
            var result = await plus.PostAsJsonAsync("/api/mail/inbox", new {
                publicKeyBase64 = publicKey });
            Assert.Equal(HttpStatusCode.OK, result.StatusCode);
            var inbox = await result.Content.ReadFromJsonAsync<MailInboxView>();
            Assert.NotNull(inbox);
            Assert.StartsWith("r", inbox.Address);
            Assert.EndsWith("@inbox.example.test", inbox.Address);
            Assert.True(inbox.PublicKeyRegistered);

            var invalidChange = await plus.PostAsJsonAsync("/api/mail/inbox", new {
                publicKeyBase64 = Convert.ToBase64String(RSA.Create(3072).ExportSubjectPublicKeyInfo()) });
            Assert.Equal(HttpStatusCode.Conflict, invalidChange.StatusCode);

            Assert.Equal(HttpStatusCode.Unauthorized,
                (await gateway.GetAsync("/internal/mail/recipient?recipient=" +
                    Uri.EscapeDataString(inbox.Address))).StatusCode);
            gateway.DefaultRequestHeaders.Add("X-GTM-Mail-Token",
                "inbound-mail-only-token-more-than-32-characters");
            Assert.Equal(HttpStatusCode.OK,
                (await gateway.GetAsync("/internal/mail/recipient?recipient=" +
                    Uri.EscapeDataString(inbox.Address))).StatusCode);

            var raw = Encoding.UTF8.GetBytes("From: Sender <sender@example.com>\r\n" +
                "Subject: Gmail forwarding verification\r\n" +
                "MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" +
                "<p>Confirm forwarding: <a href=\"https://mail.google.com/verify?code=123\">Verify</a></p>\r\n");
            async Task<HttpResponseMessage> DeliverAsync(string recipient)
            {
                using var body = new ByteArrayContent(raw);
                body.Headers.ContentType = new MediaTypeHeaderValue("message/rfc822");
                return await gateway.PostAsync("/internal/mail/receive?recipient=" +
                    Uri.EscapeDataString(recipient), body);
            }
            using (var accepted = await DeliverAsync(inbox.Address))
                Assert.Equal(HttpStatusCode.OK, accepted.StatusCode);
            using (var duplicate = await DeliverAsync(inbox.Address))
            {
                Assert.Equal(HttpStatusCode.OK, duplicate.StatusCode);
                Assert.Contains("duplicate", await duplicate.Content.ReadAsStringAsync());
            }
            Assert.Equal(HttpStatusCode.NotFound,
                (await DeliverAsync("unknown@other-domain.test")).StatusCode);

            var envelopes = await plus.GetFromJsonAsync<MailEnvelope[]>("/api/mail/messages");
            Assert.NotNull(envelopes);
            var message = Assert.Single(envelopes);
            Assert.DoesNotContain("mail.google.com", message.Ciphertext);
            var encrypted = new EncryptedInboundMail {
                WrappedKey = Convert.FromBase64String(message.WrappedKey),
                Nonce = Convert.FromBase64String(message.Nonce),
                Tag = Convert.FromBase64String(message.Tag),
                Ciphertext = Convert.FromBase64String(message.Ciphertext)
            };
            var decrypted = MailEncryption.DecryptForTestOnly(encrypted, rsa);
            Assert.Contains("mail.google.com/verify?code=123", decrypted.Body);
            Assert.Contains("sender@example.com", decrypted.From);

            var wrongRotation = await plus.PostAsJsonAsync("/api/mail/inbox/rotate-address",
                new { password = "wrong" });
            Assert.Equal(HttpStatusCode.Unauthorized, wrongRotation.StatusCode);
            var rotation = await plus.PostAsJsonAsync("/api/mail/inbox/rotate-address",
                new { password = "plus-account-password-long" });
            Assert.Equal(HttpStatusCode.OK, rotation.StatusCode);
            var newer = await rotation.Content.ReadFromJsonAsync<MailInboxView>();
            Assert.NotNull(newer);
            Assert.NotEqual(inbox.Address, newer.Address);
            Assert.Equal(HttpStatusCode.NotFound,
                (await gateway.GetAsync("/internal/mail/recipient?recipient=" +
                    Uri.EscapeDataString(inbox.Address))).StatusCode);

            using (var ack = await plus.DeleteAsync("/api/mail/messages/" + message.Id))
                Assert.Equal(HttpStatusCode.NoContent, ack.StatusCode);
            Assert.Empty((await plus.GetFromJsonAsync<MailEnvelope[]>("/api/mail/messages"))!);
            // ACK is a receipt, not an immediate destructive delete.
            var archive = await plus.GetFromJsonAsync<MailEnvelope[]>(
                "/api/mail/messages/archived?skip=0");
            Assert.NotNull(archive);
            var archived = Assert.Single(archive);
            Assert.Equal(message.Id, archived.Id);
            Assert.Equal(message.Ciphertext, archived.Ciphertext);
            using (var secondAck = await plus.DeleteAsync("/api/mail/messages/" + message.Id))
                Assert.Equal(HttpStatusCode.NoContent, secondAck.StatusCode);
            Assert.Single((await plus.GetFromJsonAsync<MailEnvelope[]>(
                "/api/mail/messages/archived?skip=0"))!);
            Assert.Empty((await mini.GetFromJsonAsync<MailEnvelope[]>(
                "/api/mail/messages/archived?skip=0"))!);
            Assert.Equal(HttpStatusCode.BadRequest,
                (await plus.GetAsync("/api/mail/messages/archived?skip=-2")).StatusCode);

        }
        finally
        {
            foreach (var key in keys) Environment.SetEnvironmentVariable(key, previous[key]);
            foreach (var name in new[] { path, states })
                foreach (var suffix in new[] { "", "-wal", "-shm" })
                    if (File.Exists(name + suffix)) File.Delete(name + suffix);
        }
    }
}
