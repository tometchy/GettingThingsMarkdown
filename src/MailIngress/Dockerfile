FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src
COPY src/MailIngress/GettingThingsMarkdown.MailIngress.csproj src/MailIngress/
RUN dotnet restore src/MailIngress/GettingThingsMarkdown.MailIngress.csproj
COPY src/MailIngress/ src/MailIngress/
RUN dotnet publish src/MailIngress/GettingThingsMarkdown.MailIngress.csproj -c Release --no-restore -o /publish

FROM mcr.microsoft.com/dotnet/runtime:10.0
RUN useradd --system --uid 10001 --create-home smtp
WORKDIR /app
COPY --from=build /publish/ ./
USER 10001:10001
EXPOSE 2525
ENTRYPOINT ["dotnet", "GettingThingsMarkdown.MailIngress.dll"]
