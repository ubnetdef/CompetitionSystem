[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9.-]+$')]
    [string]$TargetHost,

    [Parameter(Mandatory = $true)]
    [string]$SshKeyPath,

    [ValidatePattern('^[a-z_][a-z0-9_-]*$')]
    [string]$TargetUser = "compadmin",

    [string]$RemoteProjectPath
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RemoteProjectPath)) {
    $RemoteProjectPath = "/home/$TargetUser/CompetitionSystem"
}
if ($RemoteProjectPath -notmatch '^/[A-Za-z0-9._/-]+$') {
    throw "RemoteProjectPath must be an absolute path containing only letters, numbers, dots, underscores, slashes, and hyphens."
}

$SshKeyPath = [System.IO.Path]::GetFullPath($SshKeyPath)
if (-not (Test-Path -LiteralPath $SshKeyPath -PathType Leaf)) {
    throw "SSH private key was not found at '$SshKeyPath'."
}

foreach ($command in @("docker", "ssh", "scp")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command '$command' was not found. Install Docker Desktop and the Windows OpenSSH client."
    }
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $Executable @Arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "$Executable failed with exit code $exitCode."
    }
}

function Get-DotEnvRawValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Content,

        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $pattern = "(?m)^\s*$([regex]::Escape($Name))\s*=(.*)$"
    $match = [regex]::Match($Content, $pattern)
    if (-not $match.Success) {
        throw "The local .env file is missing '$Name'."
    }
    return $match.Groups[1].Value.Trim()
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$localEnvPath = Join-Path $projectRoot ".env"
$restoreScript = Join-Path $PSScriptRoot "restore_competition.sh"
if (-not (Test-Path -LiteralPath $localEnvPath -PathType Leaf)) {
    throw "The local .env file was not found. Run this from a configured CompetitionSystem checkout."
}
if (-not (Test-Path -LiteralPath $restoreScript -PathType Leaf)) {
    throw "The remote restore helper '$restoreScript' is missing."
}

$envContent = Get-Content -LiteralPath $localEnvPath -Raw
$secretKey = Get-DotEnvRawValue -Content $envContent -Name "SECRET_KEY"
$credentialKey = Get-DotEnvRawValue -Content $envContent -Name "CHECK_CREDENTIAL_KEY"
$normalizedCredentialKey = $credentialKey.Trim('"').Trim("'")
if ($normalizedCredentialKey -notmatch '^[A-Fa-f0-9]{64}$') {
    throw "The local CHECK_CREDENTIAL_KEY must contain exactly 64 hexadecimal characters."
}

$confirmation = Read-Host "Type SEND to prepare a full local database import for $TargetUser@$TargetHost"
if ($confirmation -cne "SEND") {
    Write-Host "Import cancelled."
    return
}

$sshCommon = @(
    "-i", $SshKeyPath,
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "ConnectTimeout=10"
)
$remoteTarget = "$TargetUser@$TargetHost"
$importId = [guid]::NewGuid().ToString("N")
$remoteStage = "/home/$TargetUser/.competition-import-$importId"
$remoteBackupDir = "/home/$TargetUser/competition-backups/imports"
$remoteDump = "$remoteStage/competition.dump"
$remoteKeys = "$remoteStage/import-keys.env"
$remoteRestoreScript = "$remoteStage/restore_competition.sh"
$containerDumpPath = "/tmp/competition-import-$importId.dump"
$tempDirectory = Join-Path ([System.IO.Path]::GetTempPath()) "CompetitionSystemImport-$importId"
$localDumpPath = Join-Path $tempDirectory "competition.dump"
$localKeysPath = Join-Path $tempDirectory "import-keys.env"
$localRestoreScriptPath = Join-Path $tempDirectory "restore_competition.sh"
$containerDumpCreated = $false
$remoteStageCreated = $false

Push-Location $projectRoot
try {
    New-Item -ItemType Directory -Path $tempDirectory -Force | Out-Null
    [System.IO.File]::WriteAllLines(
        $localKeysPath,
        @("SECRET_KEY=$secretKey", "CHECK_CREDENTIAL_KEY=$credentialKey"),
        [System.Text.Encoding]::ASCII
    )
    $restoreContent = [System.IO.File]::ReadAllText($restoreScript).Replace("`r`n", "`n").Replace("`r", "`n")
    [System.IO.File]::WriteAllText(
        $localRestoreScriptPath,
        $restoreContent,
        [System.Text.UTF8Encoding]::new($false)
    )

    Write-Host "Starting the local Compose database if needed."
    Invoke-CheckedCommand -Executable "docker" -Arguments @("compose", "up", "-d", "database")

    Write-Host "Checking that the local database contains competition and team data."
    Invoke-CheckedCommand -Executable "docker" -Arguments @(
        "compose", "exec", "-T", "database", "pg_isready",
        "-U", "competition", "-d", "competition"
    )
    $sourceCounts = & docker compose exec -T database psql `
        -U competition -d competition -tA -F "|" `
        -c "SELECT (SELECT COUNT(*) FROM competitions), (SELECT COUNT(*) FROM teams);"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not verify the local competition database."
    }
    $countParts = "$sourceCounts".Trim() -split "\|"
    if ($countParts.Count -ne 2 -or [int]$countParts[0] -lt 1 -or [int]$countParts[1] -lt 1) {
        throw "The local database has no competition or team records; refusing to import an empty setup."
    }

    Write-Host "Creating a full PostgreSQL snapshot from the local Compose database."
    Invoke-CheckedCommand -Executable "docker" -Arguments @(
        "compose", "exec", "-T", "database", "pg_dump",
        "-Fc", "-U", "competition", "-d", "competition", "-f", $containerDumpPath
    )
    $containerDumpCreated = $true
    Invoke-CheckedCommand -Executable "docker" -Arguments @(
        "compose", "cp", "database:$containerDumpPath", $localDumpPath
    )
    if (-not (Test-Path -LiteralPath $localDumpPath -PathType Leaf) -or
        (Get-Item -LiteralPath $localDumpPath).Length -eq 0) {
        throw "The local database export is empty; refusing to transfer it."
    }

    $createStageCommand = "mkdir -p $remoteStage && chmod 700 $remoteStage"
    Invoke-CheckedCommand -Executable "ssh" -Arguments ($sshCommon + @($remoteTarget, $createStageCommand))
    $remoteStageCreated = $true
    Invoke-CheckedCommand -Executable "scp" -Arguments (
        $sshCommon + @($localDumpPath, $localKeysPath, $localRestoreScriptPath, "${remoteTarget}:$remoteStage/")
    )

    $restoreCommand = "bash $remoteRestoreScript $RemoteProjectPath $remoteDump $remoteKeys $remoteBackupDir $remoteTarget"
    Invoke-CheckedCommand -Executable "ssh" -Arguments (
        $sshCommon + @("-tt", $remoteTarget, $restoreCommand)
    )

    Write-Host "Competition import completed. The VM database and environment backups are under $remoteBackupDir."
}
finally {
    if ($remoteStageCreated) {
        $cleanupCommand = "rm -f -- $remoteDump $remoteKeys $remoteRestoreScript && rmdir -- $remoteStage"
        & ssh @sshCommon $remoteTarget $cleanupCommand
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Could not remove temporary import files from '$remoteStage'. Remove that directory after checking that no import is running."
        }
    }
    if ($containerDumpCreated) {
        & docker compose exec -T database rm -f $containerDumpPath
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Could not remove the temporary local database export from the database container."
        }
    }
    Pop-Location
    if (Test-Path -LiteralPath $tempDirectory) {
        Remove-Item -LiteralPath $tempDirectory -Recurse -Force
    }
}
