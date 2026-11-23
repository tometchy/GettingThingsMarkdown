using GettingThingsMarkdown.ControlPlane;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.FileProviders;

namespace GettingThingsMarkdown.Tests;

public sealed class AdminDefaultDenyTests
{
    private sealed class TestHost(string environment) : IHostEnvironment
    {
        public string EnvironmentName { get; set; } = environment;
        public string ApplicationName { get; set; } = "tests";
        public string ContentRootPath { get; set; } = "/";
        public IFileProvider ContentRootFileProvider { get; set; } =
            new NullFileProvider();
    }

    private static HttpContext Context(string environment, string token)
    {
        var services = new ServiceCollection()
            .AddSingleton<IHostEnvironment>(new TestHost(environment))
            .BuildServiceProvider();
        var ctx = new DefaultHttpContext { RequestServices = services };
        ctx.Request.Headers["X-Admin-Token"] = token;
        return ctx;
    }

    [Fact]
    public void ProductionDisablesAdminEndpointsEvenWithCorrectToken()
    {
        var config = new ConfigurationBuilder().AddInMemoryCollection(
            new Dictionary<string, string?> { ["GTM_ADMIN_TOKEN"] = "test-token" }).Build();
        Assert.False(AdminSecurity.IsAuthorized(Context("Production", "test-token"), config));
    }

    [Fact]
    public void ExplicitProductionEnableStillRequiresCorrectToken()
    {
        var config = new ConfigurationBuilder().AddInMemoryCollection(
            new Dictionary<string, string?> {
                ["GTM_ADMIN_TOKEN"] = "test-token",
                ["GTM_ADMIN_API_ENABLED"] = "true"
            }).Build();
        Assert.True(AdminSecurity.IsAuthorized(Context("Production", "test-token"), config));
        Assert.False(AdminSecurity.IsAuthorized(Context("Production", "wrong"), config));
    }

    [Fact]
    public void ExistingDeveloperWorkflowStillRequiresToken()
    {
        var config = new ConfigurationBuilder().AddInMemoryCollection(
            new Dictionary<string, string?> { ["GTM_ADMIN_TOKEN"] = "dev-only" }).Build();
        Assert.True(AdminSecurity.IsAuthorized(Context("Development", "dev-only"), config));
        Assert.False(AdminSecurity.IsAuthorized(Context("Development", "other"), config));
    }
}
