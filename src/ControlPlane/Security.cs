using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Security.Cryptography;
using System.Text;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.IdentityModel.Tokens;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

namespace GettingThingsMarkdown.ControlPlane;

public sealed class TokenService
{
    private readonly SymmetricSecurityKey _key;
    public const string Issuer = "gettingthingsmarkdown";

    public TokenService(string secret)
    {
        if (Encoding.UTF8.GetByteCount(secret) < 32)
            throw new InvalidOperationException("GTM_JWT_SECRET must be at least 32 bytes.");
        _key = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(secret));
    }

    public TokenValidationParameters Validation => new()
    {
        ValidateIssuer = true, ValidIssuer = Issuer,
        ValidateAudience = true, ValidAudience = Issuer,
        ValidateLifetime = true, ValidateIssuerSigningKey = true,
        IssuerSigningKey = _key,
        ClockSkew = TimeSpan.FromMinutes(1)
    };

    public string Issue(Account account)
    {
        var token = new JwtSecurityToken(
            issuer: Issuer, audience: Issuer,
            claims: [new Claim(JwtRegisteredClaimNames.Sub, account.Id.ToString()),
                     new Claim(ClaimTypes.NameIdentifier, account.Id.ToString())],
            notBefore: DateTime.UtcNow,
            expires: DateTime.UtcNow.AddHours(12),
            signingCredentials: new SigningCredentials(_key, SecurityAlgorithms.HmacSha256));
        return new JwtSecurityTokenHandler().WriteToken(token);
    }
}

public static class AdminSecurity
{
    public static bool IsAuthorized(HttpContext context, IConfiguration configuration)
    {
        // Production operator endpoints are OFF by default, regardless of a
        // valid static token. Only enable behind private SSO/MFA ingress.
        var env = context.RequestServices.GetService<IHostEnvironment>();
        if (env?.IsDevelopment() != true &&
            !configuration.GetValue<bool>("GTM_ADMIN_API_ENABLED"))
            return false;
        var expected = configuration["GTM_ADMIN_TOKEN"];
        var provided = context.Request.Headers["X-Admin-Token"].ToString();
        if (string.IsNullOrEmpty(expected) || string.IsNullOrEmpty(provided))
            return false;
        byte[] a = SHA256.HashData(Encoding.UTF8.GetBytes(expected));
        byte[] b = SHA256.HashData(Encoding.UTF8.GetBytes(provided));
        return CryptographicOperations.FixedTimeEquals(a, b);
    }

    public static Guid? UserId(ClaimsPrincipal user)
        => Guid.TryParse(user.FindFirstValue(ClaimTypes.NameIdentifier), out var id) ? id : null;
}
