param(
    [Parameter(Mandatory)][string]$Psql,
    [Parameter(Mandatory)][ValidateRange(1, 65535)][int]$Port,
    [Parameter(Mandatory)][ValidatePattern('^synthetic_go_standalone_[a-z0-9_]+$')][string]$Database,
    [Parameter(Mandatory)][ValidatePattern('^synthetic_[a-z0-9_]+$')][string]$Template
)

$ErrorActionPreference = 'Stop'
$connection = @('-X', '-v', 'ON_ERROR_STOP=1', '-h', '127.0.0.1', '-p', [string]$Port, '-U', 'synthetic_owner')
$env:PGCLIENTENCODING = 'UTF8'
$marker = & $Psql @connection -d $Template -At -c 'SELECT kind FROM board_app.environment'
if ($LASTEXITCODE -ne 0 -or $marker -cne 'synthetic-only') {
    throw 'The template must be an isolated synthetic fixture.'
}
$fixturePassword = $env:SYNTHETIC_FIXTURE_PASSWORD
if ([string]::IsNullOrEmpty($fixturePassword)) {
    throw 'Set SYNTHETIC_FIXTURE_PASSWORD to a password for this disposable fixture.'
}

& $Psql @connection -d postgres -c "CREATE DATABASE $Database TEMPLATE $Template"
if ($LASTEXITCODE -ne 0) {
    throw 'Could not create a fresh synthetic database. Use a new database name.'
}
& $Psql @connection -d $Database -f (Join-Path $PSScriptRoot 'standalone.sql')
if ($LASTEXITCODE -ne 0) {
    throw 'Could not seed the synthetic standalone fixture.'
}

$salt = [Convert]::ToHexString([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(16)).ToLowerInvariant()
$digest = [System.Security.Cryptography.Rfc2898DeriveBytes]::Pbkdf2(
    [Text.Encoding]::UTF8.GetBytes($fixturePassword), [Text.Encoding]::UTF8.GetBytes($salt),
    600000, [System.Security.Cryptography.HashAlgorithmName]::SHA256, 32
)
$encoded = 'pbkdf2_sha256$600000$' + $salt + '$' + [Convert]::ToHexString($digest).ToLowerInvariant()
$install = @"
UPDATE board_candidate.auth_users SET password_hash='$encoded'
WHERE id IN (9101,9102,9103) AND (SELECT kind FROM board_app.environment)='synthetic-only';
"@
$install | & $Psql @connection -d $Database -q
if ($LASTEXITCODE -ne 0) {
    throw 'Could not set a password for the synthetic users.'
}
