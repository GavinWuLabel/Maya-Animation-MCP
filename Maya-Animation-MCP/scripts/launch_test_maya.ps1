param([string]$MayaExe)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'maya_executable.ps1')
$MayaExe = Resolve-MayaExecutable -ExplicitPath $MayaExe -ExecutableName 'maya.exe'
$env:MAYA_MCP_TEST_GUI = '1'
$env:MAYA_MCP_TEST_RUN = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$testRoot = Join-Path $projectRoot ('evidence\gui\' + $env:MAYA_MCP_TEST_RUN)
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$fixtureMel = Join-Path $testRoot 'start_fixture.mel'
$fixturePath = (Join-Path $projectRoot 'tests\maya_fixture.py').Replace('\','/')
$pathLiteral = ConvertTo-Json -InputObject $fixturePath -Compress
$pythonCommand = 'import runpy; runpy.run_path(' + $pathLiteral + ')'
$melLiteral = ConvertTo-Json -InputObject $pythonCommand -Compress
Set-Content -LiteralPath $fixtureMel -Value ('python(' + $melLiteral + ');') -Encoding utf8
$process = Start-Process -FilePath $MayaExe -ArgumentList @('-script', ('"' + $fixtureMel + '"'), '-log', ('"' + (Join-Path $testRoot 'maya.log') + '"')) -WindowStyle Hidden -PassThru
Write-Output ('Isolated Maya GUI fixture process: ' + $process.Id)
