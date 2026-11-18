# ops/check-quality.ps1
# Единый Windows quality gate; -Integration дополнительно запускает изолированную PostgreSQL.
param([switch]$Integration)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force .tmp/logs,.tmp/temp,.tmp/reports,.tmp/coverage,.tmp/pycache,.tmp/browser,.tmp/pytest_cache | Out-Null
$env:UV_CACHE_DIR = Join-Path $PWD '.tmp/uv-cache'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $PWD '.tmp/venv'
$env:PYTHONPYCACHEPREFIX = Join-Path $PWD '.tmp/pycache'
$env:TEMP = Join-Path $PWD '.tmp/temp'
$env:TMP = $env:TEMP
$runUser = [Environment]::UserName -replace '[^a-zA-Z0-9_.-]', '_'
function Invoke-Checked {
    # Немедленно прерывает quality gate при ненулевом коде нативной команды.
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Сбой $Program, код $LASTEXITCODE" }
}
Invoke-Checked 'uv' @('sync', '--frozen', '--python', '3.12')
$python = Join-Path $PWD '.tmp/venv/Scripts/python.exe'
Invoke-Checked $python @('-m', 'ruff', 'check', 'app', 'tests', 'migrations')
Invoke-Checked $python @('-m', 'ruff', 'format', '--check', 'app', 'tests', 'migrations')
Invoke-Checked $python @('-m', 'pytest', "--basetemp=.tmp/temp/pytest-$runUser", '-o', "cache_dir=.tmp/pytest_cache/windows-$runUser", '--junitxml=.tmp/reports/pytest-windows.xml', '--cov=app', '--cov-report=term', '--cov-report=xml')
Invoke-Checked 'git' @('-c', "safe.directory=$($PWD.Path.Replace('\','/'))", 'diff', '--check')
if ($Integration) {
    $composeArgs = @('compose', '-f', 'compose.test.yaml')
    try {
        Invoke-Checked 'docker' ($composeArgs + @('config', '--quiet'))
        Invoke-Checked 'docker' ($composeArgs + @('build', 'tests'))
        Invoke-Checked 'docker' ($composeArgs + @('up', '-d', '--wait', '--wait-timeout', '120', 'test-postgres'))
        Invoke-Checked 'docker' ($composeArgs + @('run', '--rm', '--no-deps', 'tests'))
    } finally {
        & docker @composeArgs down
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось завершить временный test stack' }
    }
}
Write-Host 'Все запрошенные проверки прошли.'
