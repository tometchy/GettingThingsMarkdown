using System.ComponentModel.DataAnnotations;
using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Text;
using System.Text.Json;
using System.Threading.RateLimiting;
using GettingThingsMarkdown.ControlPlane;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Identity;
using Microsoft.AspNetCore.RateLimiting;
using Microsoft.EntityFrameworkCore;

var builder = WebApplication.CreateBuilder(args);

string? jwtSecret = builder.Configuration["GTM_JWT_SECRET"];
if (string.IsNullOrWhiteSpace(jwtSecret))
    throw new InvalidOperationException("Set GTM_JWT_SECRET to a random string of at least 32 bytes.");
var tokenService = new TokenService(jwtSecret);
builder.Services.AddSingleton(tokenService);
builder.Services.AddSingleton<IPasswordHasher<Account>, PasswordHasher<Account>>();
builder.Services.AddDbContext<AppDb>(o => o.UseSqlite(
    builder.Configuration["GTM_DB"] ?? "Data Source=gtm.sqlite"));
builder.Services.AddDbContext<ProvisioningDb>(o => o.UseSqlite(
    builder.Configuration["GTM_PROVISIONING_DB"] ?? "Data Source=gtm-provisioning.sqlite"));
builder.Services.AddHostedService<ProvisioningWorker>();
builder.Services.AddHttpClient<StripeGateway>(client => client.Timeout = TimeSpan.FromSeconds(20));
builder.Services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
    .AddJwtBearer(o => o.TokenValidationParameters = tokenService.Validation);
builder.Services.AddAuthorization();
builder.Services.AddRateLimiter(o =>
{
    o.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
    o.AddPolicy("auth", context => RateLimitPartition.GetFixedWindowLimiter(
        context.Connection.RemoteIpAddress?.ToString() ?? "unknown", _ =>
            new FixedWindowRateLimiterOptions
            {
                PermitLimit = 10, Window = TimeSpan.FromMinutes(1), QueueLimit = 0
            }));
});
builder.Services.AddCors(o => o.AddPolicy("browser", policy =>
{
    var allowed = builder.Configuration["GTM_WEB_ORIGIN"];
    if (!string.IsNullOrWhiteSpace(allowed))
        policy.WithOrigins(allowed).AllowAnyHeader().AllowAnyMethod();
}));

var app = builder.Build();
if (!app.Environment.IsDevelopment()) app.UseHsts();
app.UseHttpsRedirection();
app.UseCors("browser");
app.UseRateLimiter();
app.UseAuthentication();
app.UseAuthorization();
app.UseDefaultFiles();
app.UseStaticFiles();

using (var scope = app.Services.CreateScope())
{
    var accountsDb = scope.ServiceProvider.GetRequiredService<AppDb>();
    await accountsDb.Database.EnsureCreatedAsync();
    // EnsureCreated does not migrate pre-existing SQLite schemas. On an old
    // database, retain every existing subscriber's original quota before
    // activating the new 10/75/500 GiB catalog. This operation is idempotent.
    var connection = accountsDb.Database.GetDbConnection();
    await accountsDb.Database.OpenConnectionAsync();
    bool quotaColumnExists = false;
    using (var inspect = connection.CreateCommand())
    {
        inspect.CommandText = "PRAGMA table_info('Accounts')";
        using var reader = await inspect.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            if (string.Equals(reader.GetString(1), "QuotaOverrideBytes",
                StringComparison.OrdinalIgnoreCase)) quotaColumnExists = true;
        }
    }
    if (!quotaColumnExists)
    {
        await accountsDb.Database.ExecuteSqlRawAsync(
            "ALTER TABLE Accounts ADD COLUMN QuotaOverrideBytes INTEGER NULL");
        await accountsDb.Database.ExecuteSqlRawAsync(
            "UPDATE Accounts SET QuotaOverrideBytes = CASE Plan " +
            "WHEN 'basic' THEN 16106127360 WHEN 'plus' THEN 214748364800 " +
            "WHEN 'pro' THEN 1099511627776 ELSE NULL END " +
            "WHERE QuotaOverrideBytes IS NULL");
    }
    await accountsDb.Database.CloseConnectionAsync();
    await scope.ServiceProvider.GetRequiredService<ProvisioningDb>().Database.EnsureCreatedAsync();
}

var clock = TimeProvider.System;
app.MapGet("/healthz", () => Results.Ok(new { status = "ok" }));
app.MapGet("/api/plans", () => Results.Ok(PlanCatalog.Tiers.Values.Select(p =>
    new { id = p.Id, name = p.Name, quotaBytes = p.QuotaBytes,
        currency = "usd", monthlyCents = p.MonthlyUsdCents,
        annualCents = p.AnnualUsdCents, trialMonths = 1 })));

app.MapPost("/api/auth/register", async (RegisterRequest input, AppDb db,
    IPasswordHasher<Account> hasher, TokenService tokens, CancellationToken ct) =>
{
    var email = (input.Email ?? "").Trim().ToLowerInvariant();
    var plan = (input.Plan ?? "").Trim().ToLowerInvariant();
    if (!new EmailAddressAttribute().IsValid(email) || email.Length > 254 ||
        input.Password is null || input.Password.Length < 12 || input.Password.Length > 1024 ||
        !PlanCatalog.IsValid(plan))
        return Results.BadRequest(new { error = "Invalid email, plan or password (minimum 12 characters)." });

    var now = clock.GetUtcNow();
    bool longTrial = app.Environment.IsDevelopment() &&
        builder.Configuration.GetValue<bool>("GTM_DEV_LONG_TRIAL");
    var account = new Account
    {
        Email = email, Plan = plan, CreatedUtc = now,
        TrialUntilUtc = TrialPolicy.NewTrialEnd(now, app.Environment.IsDevelopment(), longTrial)
    };
    account.PasswordHash = hasher.HashPassword(account, input.Password);
    db.Accounts.Add(account);
    try { await db.SaveChangesAsync(ct); }
    catch (DbUpdateException) { return Results.Conflict(new { error = "Account already exists." }); }
    return Results.Created("/api/account", new AuthResponse(tokens.Issue(account),
        PlanCatalog.View(account, now)));
}).RequireRateLimiting("auth");

app.MapPost("/api/auth/login", async (LoginRequest input, AppDb db,
    IPasswordHasher<Account> hasher, TokenService tokens, CancellationToken ct) =>
{
    var email = (input.Email ?? "").Trim().ToLowerInvariant();
    var account = await db.Accounts.SingleOrDefaultAsync(a => a.Email == email, ct);
    if (account is null || input.Password is null ||
        hasher.VerifyHashedPassword(account, account.PasswordHash, input.Password)
            == PasswordVerificationResult.Failed)
        return Results.Unauthorized();
    return Results.Ok(new AuthResponse(tokens.Issue(account),
        PlanCatalog.View(account, clock.GetUtcNow())));
}).RequireRateLimiting("auth");

var authenticated = app.MapGroup("/api").RequireAuthorization();
authenticated.MapGet("/account", async (ClaimsPrincipal user, AppDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var account = id is null ? null : await db.Accounts.FindAsync([id.Value], ct);
    return account is null ? Results.NotFound() :
        Results.Ok(PlanCatalog.View(account, clock.GetUtcNow()));
});

authenticated.MapGet("/tenant", async (ClaimsPrincipal user, ProvisioningDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    if (id is null) return Results.Unauthorized();
    var tenant = await db.Tenants.FindAsync([id.Value], ct);
    return Results.Ok(tenant is null
        ? new TenantStatusView("Pending", null, "Provisioning not started.", TenantNames.FolderId, null)
        : new TenantStatusView(tenant.Status, tenant.ServerDeviceId,
            tenant.Status == "Failed" ? tenant.LastError : null, TenantNames.FolderId,
            builder.Configuration["GTM_SYNC_PUBLIC_ADDRESS"]));
});

authenticated.MapPost("/devices", async (AddDeviceRequest input, ClaimsPrincipal user,
    ProvisioningDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var tenant = id is null ? null : await db.Tenants.FindAsync([id.Value], ct);
    if (tenant is null || tenant.Status != "Ready") return Results.Problem(
        "Tenant not yet ready.", statusCode: 409);
    if (!builder.Configuration.GetValue<bool>("GTM_KUBE_ENABLED"))
        return Results.Problem("Kubernetes not enabled.", statusCode: 503);
    using var kube = new KubeApi();
    var credentials = await kube.SecretAsync(tenant.Namespace, "gtm-credentials", ct);
    if (credentials is null) return Results.Problem("Missing tenant credentials.", statusCode: 503);
    using var syncthing = new SyncthingTenantApi(tenant.Namespace, credentials["sync-api-key"]);
    try
    {
        await syncthing.AddDeviceAsync(input.DeviceId, input.Name, ct);
        return Results.Ok(new PairResponse(TenantNames.FolderId,
            tenant.ServerDeviceId ?? await syncthing.DeviceIdAsync(ct),
            builder.Configuration["GTM_SYNC_PUBLIC_ADDRESS"] ?? "",
            "Syncthing Untrusted Device Encryption; key remains on trusted clients"));
    }
    catch (ArgumentException) { return Results.BadRequest(new { error = "Invalid device ID." }); }
    catch (HttpRequestException) { return Results.Problem("Syncthing pairing failed.", statusCode: 503); }
});

authenticated.MapGet("/devices", async (ClaimsPrincipal user,
    ProvisioningDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var tenant = id is null ? null : await db.Tenants.FindAsync([id.Value], ct);
    if (tenant is null || tenant.Status != "Ready")
        return Results.Problem("Tenant not ready.", statusCode: 409);
    if (!KubeApi.Available) return Results.Problem("Kubernetes unavailable.", statusCode: 503);
    using var kube = new KubeApi();
    var credentials = await kube.SecretAsync(tenant.Namespace, "gtm-credentials", ct);
    if (credentials is null) return Results.Problem("Tenant unavailable.", statusCode: 503);
    using var sync = new SyncthingTenantApi(tenant.Namespace, credentials["sync-api-key"]);
    return Results.Ok(await sync.ListPairedDeviceIdsAsync(ct));
});

authenticated.MapDelete("/devices/{deviceId}", async (string deviceId,
    ClaimsPrincipal user, ProvisioningDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var tenant = id is null ? null : await db.Tenants.FindAsync([id.Value], ct);
    if (tenant is null || tenant.Status != "Ready")
        return Results.Problem("Tenant not ready.", statusCode: 409);
    if (!KubeApi.Available) return Results.Problem("Kubernetes unavailable.", statusCode: 503);
    using var kube = new KubeApi();
    var credentials = await kube.SecretAsync(tenant.Namespace, "gtm-credentials", ct);
    if (credentials is null) return Results.Problem("Tenant unavailable.", statusCode: 503);
    using var sync = new SyncthingTenantApi(tenant.Namespace, credentials["sync-api-key"]);
    try { await sync.RemoveDeviceAsync(deviceId, ct); }
    catch (ArgumentException) { return Results.BadRequest(new { error = "Invalid device ID." }); }
    return Results.NoContent();
});

// Gitea is never reachable directly from untrusted clients. The API exposes
// only bounded read-only encrypted Git objects belonging to the current account.
async Task<IResult> GetHistoryAsync(ClaimsPrincipal user, string? sha, string kind,
    ProvisioningDb states, CancellationToken ct)
{
    var id = AdminSecurity.UserId(user);
    if (id is null) return Results.Unauthorized();
    var tenant = await states.Tenants.FindAsync([id.Value], ct);
    if (tenant is null || tenant.Status is not ("Ready" or "Suspended"))
        return Results.Problem("History tenant not ready.", statusCode: 409);
    if (sha is not null && !GitHistoryProxy.ValidGitOid(sha))
        return Results.BadRequest(new { error = "Invalid Git SHA." });
    if (!KubeApi.Available)
        return Results.Problem("History access requires Kubernetes.", statusCode: 503);
    using var kube = new KubeApi();
    var cert = await kube.SecretAsync(tenant.Namespace, "history-ca", ct);
    var secrets = await kube.SecretAsync(tenant.Namespace, "gtm-credentials", ct);
    if (cert is null || secrets is null)
        return Results.Problem("Missing tenant history credentials.", statusCode: 503);
    using var history = new GitHistoryProxy(tenant.Namespace,
        cert["tls.crt"], secrets["admin-pass"]);
    try
    {
        string body = kind switch
        {
            "commits" => await history.RecentCommitsAsync(ct),
            "tree" => await history.TreeAsync(sha!, ct),
            "blob" => await history.EncryptedBlobAsync(sha!, ct),
            _ => throw new InvalidOperationException("Unknown history action")
        };
        return Results.Content(body, "application/json");
    }
    catch (HttpRequestException)
    {
        return Results.Problem("Encrypted history unavailable.", statusCode: 503);
    }
    catch (InvalidOperationException)
    {
        return Results.Problem("Encrypted history response rejected.", statusCode: 413);
    }
}

authenticated.MapGet("/history/commits", (ClaimsPrincipal user,
    ProvisioningDb states, CancellationToken ct) =>
    GetHistoryAsync(user, null, "commits", states, ct));
authenticated.MapGet("/history/tree/{sha}", (ClaimsPrincipal user, string sha,
    ProvisioningDb states, CancellationToken ct) =>
    GetHistoryAsync(user, sha, "tree", states, ct));
authenticated.MapGet("/history/blob/{sha}", (ClaimsPrincipal user, string sha,
    ProvisioningDb states, CancellationToken ct) =>
    GetHistoryAsync(user, sha, "blob", states, ct));

authenticated.MapPost("/billing/checkout", async (CheckoutRequest request,
    ClaimsPrincipal user, AppDb db, StripeGateway stripe, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var account = id is null ? null : await db.Accounts.FindAsync([id.Value], ct);
    if (account is null) return Results.NotFound();
    var plan = (request.Plan ?? "").ToLowerInvariant();
    if (!PlanCatalog.IsValid(plan) || !PlanCatalog.ValidInterval(request.BillingInterval))
        return Results.BadRequest(new { error = "Unknown plan or billing interval." });
    if (!stripe.Configured) return Results.Problem("Stripe is not configured.", statusCode: 503);
    if (account.StripeStatus == "active" && account.PaidUntilUtc > clock.GetUtcNow())
        return Results.Conflict(new { error = "Subscription is active. Use the billing portal to change plans." });
    try { return Results.Ok(await stripe.CheckoutAsync(account, plan, request.BillingInterval, ct)); }
    catch (InvalidOperationException) { return Results.Problem("Checkout unavailable.", statusCode: 503); }
});

authenticated.MapPost("/billing/portal", async (ClaimsPrincipal user, AppDb db,
    StripeGateway stripe, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var account = id is null ? null : await db.Accounts.FindAsync([id.Value], ct);
    if (account is null) return Results.NotFound();
    if (!stripe.Configured || account.StripeCustomerId is null)
        return Results.Problem("Customer portal unavailable.", statusCode: 503);
    try { return Results.Ok(await stripe.PortalAsync(account, ct)); }
    catch (InvalidOperationException) { return Results.Problem("Customer portal unavailable.", statusCode: 503); }
});

// Emergency/admin operations require a separate server-only secret, never a user account flag.
app.MapGet("/api/admin/accounts", async (string? email, HttpContext context,
    AppDb db, CancellationToken ct) =>
{
    if (!AdminSecurity.IsAuthorized(context, builder.Configuration)) return Results.Unauthorized();
    var query = db.Accounts.AsNoTracking();
    if (!string.IsNullOrWhiteSpace(email))
    {
        var normalized = email.Trim().ToLowerInvariant();
        query = query.Where(a => a.Email == normalized);
    }
    var accounts = await query.OrderBy(x => x.Email).Take(50).ToListAsync(ct);
    return Results.Ok(accounts.Select(a => PlanCatalog.View(a, clock.GetUtcNow())));
});

app.MapGet("/api/admin/accounts/{id:guid}/audit", async (Guid id, HttpContext context,
    AppDb db, CancellationToken ct) =>
{
    if (!AdminSecurity.IsAuthorized(context, builder.Configuration)) return Results.Unauthorized();
    var entries = await db.Audit.AsNoTracking()
        .Where(x => x.AccountId == id).OrderByDescending(x => x.OccurredUtc)
        .Take(50).ToListAsync(ct);
    return Results.Ok(entries);
});

app.MapPost("/api/admin/accounts/{id:guid}/extend-trial", async (
    Guid id, ExtendTrialRequest request, HttpContext context,
    AppDb db, CancellationToken ct) =>
{
    if (!AdminSecurity.IsAuthorized(context, builder.Configuration)) return Results.Unauthorized();
    if (request.Days is < 1 or > 36500) return Results.BadRequest(new { error = "Invalid days." });
    var account = await db.Accounts.FindAsync([id], ct);
    if (account is null) return Results.NotFound();
    var now = clock.GetUtcNow();
    var before = account.TrialUntilUtc;
    account.TrialUntilUtc = TrialPolicy.Extend(before, now, request.Days);
    db.Audit.Add(new AuditEntry
    {
        AccountId = id, Action = "extend-trial", Actor = "admin-token",
        OccurredUtc = now,
        Details = $"days={request.Days};before={before:O};after={account.TrialUntilUtc:O};reason={request.Reason?.Replace(';', ',').Substring(0, Math.Min(request.Reason.Length, 120))}"
    });
    await db.SaveChangesAsync(ct);
    return Results.Ok(PlanCatalog.View(account, now));
});

// Webhooks are authoritative only after signature verification. Do not trust redirect success URLs.
app.MapPost("/api/webhooks/stripe", async (HttpContext context,
    AppDb db, StripeGateway stripe, CancellationToken ct) =>
{
    if (context.Request.ContentLength is > 1048576)
        return Results.StatusCode(413);
    using var reader = new StreamReader(context.Request.Body, Encoding.UTF8);
    var payload = await reader.ReadToEndAsync(ct);
    var signature = context.Request.Headers["Stripe-Signature"].ToString();
    if (!stripe.VerifySignature(payload, signature, clock.GetUtcNow()))
        return Results.Unauthorized();

    using var document = JsonDocument.Parse(payload);
    var root = document.RootElement;
    string? eventId = StripeGateway.String(root, "id");
    string? type = StripeGateway.String(root, "type");
    var data = StripeGateway.Nested(root, "data", "object");
    if (string.IsNullOrEmpty(eventId) || eventId.Length > 255 || data.ValueKind != JsonValueKind.Object)
        return Results.BadRequest();
    if (await db.HandledStripeEvents.AnyAsync(x => x.Id == eventId, ct))
        return Results.Ok(new { duplicate = true });

    string? customerId = StripeGateway.String(data, "customer");
    string? subscriptionId = type == "checkout.session.completed"
        ? StripeGateway.String(data, "subscription")
        : StripeGateway.String(data, "id");
    var metadataId = StripeGateway.String(StripeGateway.Nested(data, "metadata"), "gtm_account_id");
    var checkoutId = type == "checkout.session.completed"
        ? StripeGateway.String(data, "client_reference_id") : null;
    Guid? accountId = Guid.TryParse(metadataId ?? checkoutId, out var parsed) ? parsed : null;
    Account? account = accountId is not null
        ? await db.Accounts.FindAsync([accountId.Value], ct) : null;
    if (account is null && subscriptionId is not null)
        account = await db.Accounts.FirstOrDefaultAsync(x => x.StripeSubscriptionId == subscriptionId, ct);
    if (account is null && customerId is not null)
        account = await db.Accounts.FirstOrDefaultAsync(x => x.StripeCustomerId == customerId, ct);

    if (account is not null)
    {
        if (type == "checkout.session.completed")
        {
            account.StripeCustomerId = customerId ?? account.StripeCustomerId;
            account.StripeSubscriptionId = subscriptionId ?? account.StripeSubscriptionId;
            // No entitlement granted by an unverified payment / mere checkout completion.
        }
        if (type is "customer.subscription.created" or "customer.subscription.updated")
        {
            account.StripeCustomerId = customerId ?? account.StripeCustomerId;
            account.StripeSubscriptionId = subscriptionId ?? account.StripeSubscriptionId;
            account.StripeStatus = StripeGateway.String(data, "status") ?? "unknown";
            if (account.StripeStatus == "active" && StripeGateway.PeriodEnd(data) is { } end)
                account.PaidUntilUtc = end;
            // A paid plan is selected only by a signed Stripe subscription event.
            var items = StripeGateway.Nested(data, "items", "data");
            if (account.StripeStatus == "active" && account.PaidUntilUtc > clock.GetUtcNow() &&
                items.ValueKind == JsonValueKind.Array)
                foreach (var item in items.EnumerateArray())
                {
                    var price = StripeGateway.String(StripeGateway.Nested(item, "price"), "id");
                    foreach (var plan in PlanCatalog.Quotas.Keys)
                        if (price is not null && (price == builder.Configuration["GTM_STRIPE_PRICE_" + plan.ToUpperInvariant()] ||
                            price == builder.Configuration["GTM_STRIPE_PRICE_" + plan.ToUpperInvariant() + "_MONTH"] ||
                            price == builder.Configuration["GTM_STRIPE_PRICE_" + plan.ToUpperInvariant() + "_YEAR"]))
                            account.Plan = plan;
                }
        }
        if (type == "customer.subscription.deleted")
        {
            account.StripeStatus = "canceled";
            account.PaidUntilUtc = clock.GetUtcNow();
        }
        if (type == "invoice.payment_failed")
            account.StripeStatus = "past_due";
        if (type == "invoice.paid")
        {
            // Also accept older and newer invoice subscription-reference shapes.
            var parentSubscription = StripeGateway.String(
                StripeGateway.Nested(data, "parent", "subscription_details"), "subscription");
            var invoiceSubscription = StripeGateway.String(data, "subscription") ?? parentSubscription;
            if (invoiceSubscription == account.StripeSubscriptionId)
            {
                var lines = StripeGateway.Nested(data, "lines", "data");
                if (lines.ValueKind == JsonValueKind.Array)
                    foreach (var line in lines.EnumerateArray())
                    {
                        var endJson = StripeGateway.Nested(line, "period", "end");
                        if (endJson.ValueKind == JsonValueKind.Number && endJson.TryGetInt64(out var unix))
                        {
                            var until = DateTimeOffset.FromUnixTimeSeconds(unix);
                            if (until > clock.GetUtcNow())
                            {
                                account.PaidUntilUtc = until;
                                account.StripeStatus = "active";
                            }
                        }
                    }
            }
        }
    }

    db.HandledStripeEvents.Add(new HandledStripeEvent { Id = eventId, HandledUtc = clock.GetUtcNow() });
    await db.SaveChangesAsync(ct);
    return Results.Ok();
});

app.Run();

public partial class Program { }
