using System.Text;
using GettingThingsMarkdown.Automations;
using GettingThingsMarkdown.Client.Desktop;
namespace GettingThingsMarkdown.Tests;
public sealed class MailAttachmentImporterTests
{
    [Fact]
    public async Task MaterializesExactPdfAndInlineImageWithRelativeMarkdownLinks()
    {
        string root = Path.Combine(Path.GetTempPath(), "gtmd-mail-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            var id = Guid.NewGuid();
            var ws = new MarkdownWorkspace(root);
            byte[] raw = Encoding.UTF8.GetBytes("From: sender@example.com\r\nSubject: test\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\nContent-Type: text/plain\r\n\r\ncomplete body\r\n--x\r\nContent-Type: application/pdf; name=sample.pdf\r\nContent-Disposition: attachment; filename=sample.pdf\r\nContent-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQ=\r\n--x\r\nContent-Type: image/png\r\nContent-ID: <picture>\r\nContent-Disposition: inline\r\nContent-Transfer-Encoding: base64\r\n\r\nAQIDBA==\r\n--x--\r\n");
            var result = await MailAttachmentImporter.SaveAsync(ws, id, Convert.ToBase64String(raw), default);
            Assert.Contains("complete body", result.Body);
            Assert.Contains($"../Attachments/mail-{id:N}/001-sample.pdf", result.Links);
            Assert.Contains($"![002-image.png](../Attachments/mail-{id:N}/002-image.png)", result.Links);
            Assert.Equal(Encoding.ASCII.GetBytes("%PDF-1.4"), await File.ReadAllBytesAsync(ws.ResolveFile($"Attachments/mail-{id:N}/001-sample.pdf")));
            Assert.Equal(new byte[] {1, 2, 3, 4}, await File.ReadAllBytesAsync(ws.ResolveFile($"Attachments/mail-{id:N}/002-image.png")));
        }
        finally { Directory.Delete(root, true); }
    }
}
