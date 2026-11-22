using System.Runtime.InteropServices.JavaScript;
using System.Runtime.Versioning;
using Avalonia;
using Avalonia.Browser;
using GettingThingsMarkdown.Client.Shared;

[assembly: SupportedOSPlatform("browser")]

namespace GettingThingsMarkdown.Client.Browser;

internal static partial class Program
{
    [JSImport("getOrigin", "GtmRuntime")]
    internal static partial string GetOrigin();

    public static async Task Main(string[] args)
    {
        await JSHost.ImportAsync("GtmRuntime", "./js/runtime.js");
        ApiConfiguration.BaseAddress = new Uri(GetOrigin());
        await AppBuilder.Configure<App>().StartBrowserAppAsync("out");
    }
}
