using GettingThingsMarkdown.Automations;

namespace GettingThingsMarkdown.Tests;
public sealed class MailTaskFormatterTests
{
    [Fact]
    public void UntrustedHeadersCannotInjectFrontmatterOrAlternateTasks()
    {
        var text = MailTaskFormatter.Render("Hello\nstatus: done",
            "Hacker <not-a-task>\r\nstatus: done", "Message body",
            new DateTimeOffset(2026, 11, 23, 11, 1, 2, TimeSpan.Zero));
        Assert.StartsWith("---\nstatus: todo\nsource: email\n", text);
        Assert.Contains("# Hello status: done", text);
        Assert.Contains("&lt;not-a-task&gt;", text);
        Assert.Equal(2, text.Split("status: done").Length - 1);
        Assert.Equal("ToDo/mail-8bb323fc2ae34ef6a1550c476ad1be31.md",
            MailTaskFormatter.FileName(Guid.Parse("8bb323fc-2ae3-4ef6-a155-0c476ad1be31")));
    }
}
