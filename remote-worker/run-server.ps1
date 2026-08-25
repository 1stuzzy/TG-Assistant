$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$modelArg = $args[0]
if ([string]::IsNullOrWhiteSpace($modelArg)) { $modelArg = "model.gguf" }
if ([IO.Path]::IsPathRooted($modelArg)) {
    $modelPath = $modelArg
} else {
    $modelPath = Join-Path $root $modelArg
}
if (-not (Test-Path -LiteralPath $modelPath)) {
    throw "Net fajla modeli: $modelPath"
}

function Find-Server([string]$dir) {
    if (-not (Test-Path -LiteralPath $dir)) { return $null }
    Get-ChildItem -LiteralPath $dir -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Clear-Port([int]$port) {
    $ids = @()
    try {
        $ids = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique)
    } catch { }
    if (-not $ids) {
        netstat -ano | ForEach-Object {
            if ($_ -match ":$port\s+.+LISTENING\s+(\d+)") { $ids += [int]$Matches[1] }
        }
        $ids = $ids | Select-Object -Unique
    }
    foreach ($procId in $ids) {
        if ($procId -le 4) { continue }
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if (-not $proc) { continue }
        Write-Host "Port $port zanyat $($proc.Name) PID $procId - zakryvaju staroe okno."
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

Clear-Port 8088

    Write-Host "Zapusk: $($exe.FullName)"
    Write-Host "Model:  $modelPath"
    Write-Host "GPU layers: $ngl"
    Write-Host "Ne zakryvajte okno. Dozhdites stroki pro port 8088."
    Write-Host "V paneli: http://192.168.0.88:8088"
    Write-Host ""
    Push-Location $exe.DirectoryName
    try {
        & $exe.FullName -m $modelPath --host 0.0.0.0 --port 8088 -c 2048 -ngl $ngl
        return $LASTEXITCODE
    } finally {
        Pop-Location
    }
}

$cuda = Find-Server (Join-Path $root "runtime-cuda")
$cpu = Find-Server (Join-Path $root "runtime-cpu")
if (-not $cuda -and -not $cpu) { throw "llama-server.exe ne najden. Udalyte papki runtime-* i zapustite start.bat snova." }

$code = 1
if ($cuda) {
    $code = Start-Llama $cuda 99
    if ($code -ne 0 -and $cpu) {
        Write-Host "CUDA ne zapustilas, proboju CPU..."
        $code = Start-Llama $cpu 0
    }
} else {
    $code = Start-Llama $cpu 0
}
exit $code
