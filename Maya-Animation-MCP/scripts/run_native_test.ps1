param([string]$MayaPython, [string]$ClientPython)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'maya_executable.ps1')
$MayaPython = Resolve-MayaExecutable -ExplicitPath $MayaPython
if (-not $ClientPython) { $ClientPython = Join-Path $projectRoot '.venv\Scripts\python.exe' }
$env:MAYA_MCP_TEST_GUI = '0'
$env:MAYA_MCP_TEST_RUN = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$testRoot = Join-Path $projectRoot ('evidence\native\' + $env:MAYA_MCP_TEST_RUN)
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$fixture = Join-Path $projectRoot 'tests\maya_fixture.py'
$process = Start-Process -FilePath $MayaPython -ArgumentList ('"' + $fixture + '"') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $testRoot 'maya-stdout.log') -RedirectStandardError (Join-Path $testRoot 'maya-stderr.log')
try {
    $deadline = (Get-Date).AddSeconds(45)
    $ready = Join-Path $testRoot 'ready.json'
    while (-not (Test-Path -LiteralPath $ready)) {
        if ((Get-Date) -gt $deadline -or $process.HasExited) { throw ('Maya fixture startup failed. See ' + $testRoot) }
        Start-Sleep -Milliseconds 300
    }
    & $ClientPython (Join-Path $projectRoot 'tests\integration_mcp.py') native
    if ($LASTEXITCODE -ne 0) { throw 'MCP integration test failed' }
} finally {
    Set-Content -LiteralPath (Join-Path $testRoot 'stop') -Value 'stop'
    if (-not $process.WaitForExit(10000)) { Stop-Process -InputObject $process }
}
