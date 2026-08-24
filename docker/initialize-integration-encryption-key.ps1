[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$targetPath = Join-Path $repositoryRoot ".env.docker.local"
$variableName = "INTEGRATION_ENCRYPTION_KEY"
$assignmentPattern = "^\s*" + [regex]::Escape($variableName) + "\s*="

$lines = if (Test-Path -LiteralPath $targetPath) {
    [System.IO.File]::ReadAllLines($targetPath)
}
else {
    [string[]]@()
}

$existingIndex = -1
$existingValue = ""
for ($index = 0; $index -lt $lines.Count; $index++) {
    if ($lines[$index] -match $assignmentPattern) {
        $existingIndex = $index
        $existingValue = ($lines[$index] -split "=", 2)[1].Trim().Trim('"').Trim("'")
        break
    }
}

if ($existingValue) {
    Write-Host "$variableName is already configured in .env.docker.local; no change made."
    exit 0
}

$keyBytes = [byte[]]::new(32)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($keyBytes)
$key = [Convert]::ToBase64String($keyBytes).Replace("+", "-").Replace("/", "_")
$assignment = "$variableName=$key"

if ($existingIndex -ge 0) {
    $lines[$existingIndex] = $assignment
}
else {
    if ($lines.Count -gt 0 -and $lines[-1] -ne "") {
        $lines += ""
    }
    $lines += $assignment
}

$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllLines($targetPath, $lines, $utf8WithoutBom)

Write-Host "$variableName was initialized in .env.docker.local. The secret value was not printed."
