function Resolve-MayaExecutable {
    param([string]$ExplicitPath, [string]$ExecutableName = 'mayapy.exe')
    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) { throw "Maya executable not found: $ExplicitPath" }
        return (Resolve-Path -LiteralPath $ExplicitPath).Path
    }
    if ($env:MAYA_LOCATION) {
        $candidate = Join-Path $env:MAYA_LOCATION ('bin\' + $ExecutableName)
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $command = Get-Command $ExecutableName -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $autodeskRoot = Join-Path $env:ProgramFiles 'Autodesk'
    if (Test-Path -LiteralPath $autodeskRoot) {
        foreach ($directory in (Get-ChildItem -LiteralPath $autodeskRoot -Directory -Filter 'Maya*' | Sort-Object Name -Descending)) {
            $candidate = Join-Path $directory.FullName ('bin\' + $ExecutableName)
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    throw "Cannot locate $ExecutableName. Pass the Maya executable path explicitly."
}
