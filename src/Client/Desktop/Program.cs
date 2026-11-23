using Avalonia;
using GettingThingsMarkdown.Client.Shared;

namespace GettingThingsMarkdown.Client.Desktop;

internal static class Program
{
    [STAThread]
    public static void Main(string[] args)
    {
        ClientRuntime.EmbeddedSyncEngineFactory = () => new LocalSyncthingEngine();
        AppBuilder.Configure<App>().UsePlatformDetect().StartWithClassicDesktopLifetime(args);
    }
}
