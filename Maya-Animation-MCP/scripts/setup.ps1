$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:UV_CACHE_DIR = Join-Path $projectRoot '.uv-cache'
Push-Location -LiteralPath $projectRoot
try {
    uv venv --python 3.10 .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment setup failed' }
    uv pip install --python .venv\Scripts\python.exe -e .
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    & (Join-Path $projectRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'configure.py')
    if ($LASTEXITCODE -ne 0) { throw 'Configuration snippet generation failed' }
} finally {
    Pop-Location
}
