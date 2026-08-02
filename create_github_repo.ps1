# Run after: gh auth login
$ErrorActionPreference = "Stop"
$gh = "$env:LOCALAPPDATA\gh-cli\bin\gh.exe"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Test-Path $gh)) {
    Write-Host "gh CLI not found. Install from https://cli.github.com/"
    exit 1
}

& $gh auth status
if ($LASTEXITCODE -ne 0) {
    Write-Host "Please login first:"
    Write-Host "  & `"$gh`" auth login -h github.com -p https -w"
    exit 1
}

Set-Location $repoRoot
git branch -M main

& $gh repo create hk-iphone-grab `
    --public `
    --source . `
    --remote origin `
    --description "Hong Kong Apple Store iPhone stock monitor and API grab tool" `
    --push

Write-Host ""
Write-Host "Done: https://github.com/zero850x-ctrl/hk-iphone-grab"
