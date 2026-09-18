#!/usr/bin/env pwsh

# Inbox Cleanup launcher for Windows

param(
    [switch]$Demo
)

# Change to the directory where this script lives
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Create .venv if it doesn't exist
if (-not (Test-Path ".venv")) {
    Write-Host "Creating Python virtual environment..."
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        & py -3 -m venv .venv
    }
}

# Install dependencies if needed
$DepsMarker = ".venv\.deps-installed"
$RequirementsTime = (Get-Item "requirements.txt").LastWriteTime
$DepsTime = if (Test-Path $DepsMarker) { (Get-Item $DepsMarker).LastWriteTime } else { [datetime]::MinValue }

if (-not (Test-Path $DepsMarker) -or $RequirementsTime -gt $DepsTime) {
    Write-Host "Installing dependencies..."
    & .venv\Scripts\pip install -q -r requirements.txt
    New-Item -ItemType File -Path $DepsMarker -Force | Out-Null
}

# Copy .env.example to .env if .env doesn't exist
if (-not (Test-Path ".env")) {
    Write-Host "Creating .env from .env.example..."
    Write-Host "Note: Set your ANTHROPIC_API_KEY in .env if you want AI labeling. See docs/SETUP.md for details."
    Copy-Item ".env.example" ".env"
}

# Open browser after 2 seconds in the background
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 2
    Start-Process "http://127.0.0.1:8765" -ErrorAction SilentlyContinue
} | Out-Null

# Set DEMO mode if flag was passed
if ($Demo) {
    $env:DEMO = "1"
}

# Run the app
& .venv\Scripts\python.exe -m backend.api
