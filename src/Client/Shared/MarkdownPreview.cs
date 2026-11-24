using Avalonia;
using Avalonia.Controls;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using GettingThingsMarkdown.Automations;

namespace GettingThingsMarkdown.Client.Shared;

/// <summary>A minimal offline Markdown preview with no HTML execution and no
/// remote image loading. It intentionally renders a safe text subset.</summary>
public static class MarkdownPreview
{
    public static Control Create(string markdown, MarkdownWorkspace? workspace = null,
        string? documentPath = null)
    {
        var stack = new StackPanel { Spacing = 5, Margin = new Thickness(4) };
        bool code = false;
        foreach (var line in markdown.Replace("\r\n", "\n", StringComparison.Ordinal).Split('\n').Take(2000))
        {
            if (line.StartsWith("```", StringComparison.Ordinal))
            {
                code = !code;
                continue;
            }
            // Only local, path-validated imported attachments. Never fetch
            // remote resources from untrusted e-mail/Markdown.
            if (!code && workspace is not null && documentPath is not null &&
                line.StartsWith("![", StringComparison.Ordinal) &&
                line.EndsWith(")", StringComparison.Ordinal))
            {
                var marker = line.IndexOf("](", StringComparison.Ordinal);
                if (marker > 1)
                {
                    var url = line[(marker + 2)..^1];
                    if (url.StartsWith("../Attachments/", StringComparison.Ordinal))
                    {
                        try
                        {
                            var imagePath = workspace.ResolveFile(url[3..]);
                            if (File.Exists(imagePath) && new FileInfo(imagePath).Length <= 40L * 1024 * 1024)
                            {
                                stack.Children.Add(new Image { Source = new Bitmap(imagePath),
                                    MaxHeight = 320, Stretch = Stretch.Uniform });
                                continue;
                            }
                        }
                        catch (Exception) { /* Invalid or unsupported image remains text. */ }
                    }
                }
            }
            var text = line;
            int size = 14;
            bool heading = false;
            if (!code && line.StartsWith("# ", StringComparison.Ordinal))
            { text = line[2..]; size = 26; heading = true; }
            else if (!code && line.StartsWith("## ", StringComparison.Ordinal))
            { text = line[3..]; size = 21; heading = true; }
            else if (!code && line.StartsWith("### ", StringComparison.Ordinal))
            { text = line[4..]; size = 17; heading = true; }
            else if (!code && (line.StartsWith("- ", StringComparison.Ordinal) ||
                               line.StartsWith("* ", StringComparison.Ordinal)))
                text = "• " + line[2..];
            var block = new TextBlock
            {
                Text = text, FontSize = size, TextWrapping = TextWrapping.Wrap,
                FontWeight = heading ? FontWeight.Bold : FontWeight.Normal,
                FontFamily = code ? FontFamily.Default : FontFamily.Default
            };
            stack.Children.Add(block);
        }
        return new ScrollViewer { Content = stack, MaxHeight = 400 };
    }
}
