param([string]$MayaPython, [string]$OutputRoot, [string]$WorkRoot, [string]$ConnectionFile, [switch]$AllowPython)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'maya_executable.ps1')
$mayaBinary = Resolve-MayaExecutable -ExplicitPath $MayaPython
if (-not $OutputRoot) { $OutputRoot = Join-Path $projectRoot 'output' }
if (-not $WorkRoot) { $WorkRoot = Join-Path $projectRoot 'runtime\background' }
if (-not $ConnectionFile) { $ConnectionFile = Join-Path $projectRoot 'runtime\background_connection.json' }
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
$WorkRoot = [IO.Path]::GetFullPath($WorkRoot)
$ConnectionFile = [IO.Path]::GetFullPath($ConnectionFile)
if (Test-Path -LiteralPath $ConnectionFile) { throw 'Connection metadata already exists. Verify/stop its owner before restarting; do not overwrite a live session.' }
New-Item -ItemType Directory -Path $OutputRoot,$WorkRoot -Force | Out-Null
$serviceScript = Join-Path $projectRoot 'maya_side\standalone_service.py'
$arguments = '"' + $serviceScript + '" --output-root "' + $OutputRoot + '" --work-root "' + $WorkRoot + '" --connection-file "' + $ConnectionFile + '"'
if ($AllowPython) { $arguments += ' --allow-python' }
$process = Start-Process -FilePath $mayaBinary -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $WorkRoot 'stdout.log') -RedirectStandardError (Join-Path $WorkRoot 'stderr.log')
@{ pid=$process.Id; connection_file=$ConnectionFile; work_directory=$WorkRoot; output_root=$OutputRoot; allow_python=[bool]$AllowPython } | ConvertTo-Json
