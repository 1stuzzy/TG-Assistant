$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$modelArg = $args[0]
$modeArg = $args[1]
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

function Test-GpuBinary($exe) {
    if (-not $exe) { return $false }
    $dir = $exe.DirectoryName
    return [bool](
        (Get-ChildItem -LiteralPath $dir -Filter "ggml-cuda*.dll" -ErrorAction SilentlyContinue) -or
        (Get-ChildItem -LiteralPath $dir -Filter "ggml-vulkan*.dll" -ErrorAction SilentlyContinue)
    )
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
        Write-Host "Port $port zanyat $($proc.Name) PID $procId - zakryvaju."
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

function Read-Mode {
    param([string]$preset)
    $key = ""
    if ($preset) { $key = $preset.Trim().ToLowerInvariant() }
    if ($key -in @("1", "cpu")) { return @{ Name = "CPU"; Ngl = 0 } }
    if ($key -in @("2", "gpu")) { return @{ Name = "GPU"; Ngl = 99 } }
    if ($key -in @("3", "hybrid", "cpu+gpu", "cpu-gpu")) {
        return @{ Name = "CPU+GPU"; Ngl = 24 }
    }

    Write-Host ""
    Write-Host "Kak schitat model?"
    Write-Host "  1  CPU        tolko processor"
    Write-Host "  2  GPU        vsja model na videokarte"
    Write-Host "  3  CPU+GPU    chast sloev na GPU, ostalnoe na CPU"
    Write-Host ""
    $sel = Read-Host "Vvedite 1, 2 ili 3"
    switch ($sel.Trim()) {
        "2" { return @{ Name = "GPU"; Ngl = 99 } }
        "3" {
            $raw = Read-Host "Sloev na GPU (Enter = 24; 8GB ~20-32, 12GB ~40)"
            $ngl = 24
            if ($raw -match "^\d+$") { $ngl = [Math]::Max(1, [int]$raw) }
            return @{ Name = "CPU+GPU"; Ngl = $ngl }
        }
        default { return @{ Name = "CPU"; Ngl = 0 } }
    }
}

function Start-Llama($exe, [int]$ngl, [string]$label) {
    $env:CUDA_VISIBLE_DEVICES = "0"
    $gpuOk = Test-GpuBinary $exe
    Write-Host ""
    Write-Host "Rezhim: $label   -ngl $ngl"
    Write-Host "Zapusk: $($exe.FullName)"
    Write-Host "GPU-dll ryadom s exe: $(if ($gpuOk) { 'da' } else { 'NET — eto CPU-sborka' })"
    Write-Host "Model:  $modelPath"
    Write-Host "V loge ischite: offloaded ... layers to GPU"
    Write-Host "Ne zakryvajte okno."
    Write-Host ""
    if ($ngl -gt 0 -and -not $gpuOk) {
        throw "Vybran GPU, no zapuskaetsja CPU llama-server. Skachajte CUDA/Vulkan: start.bat snova posle obnovlenija skriptov."
    }
    Push-Location $exe.DirectoryName
    try {
        & $exe.FullName -m $modelPath --host 0.0.0.0 --port 8088 -c 2048 -ngl $ngl
        return $LASTEXITCODE
    } finally {
        Pop-Location
    }
}

Clear-Port 8088

$choice = Read-Mode $modeArg
$wantGpu = $choice.Ngl -gt 0

if ($wantGpu) {
    Write-Host "Nuzhen GPU-runtime, proverjaju CUDA/Vulkan..."
    & (Join-Path $root "fetch-runtime.ps1") -NeedGpu
}

$cuda = Find-Server (Join-Path $root "runtime-cuda")
$vulkan = Find-Server (Join-Path $root "runtime-vulkan")
$cpu = Find-Server (Join-Path $root "runtime-cpu")
if (-not $cuda -and -not $vulkan -and -not $cpu) {
    throw "llama-server.exe ne najden. Udalyte papki runtime-* i zapustite start.bat snova."
}

$exe = $null
if ($wantGpu) {
    if (Test-GpuBinary $cuda) { $exe = $cuda }
    elseif (Test-GpuBinary $vulkan) { $exe = $vulkan }
    else {
        throw "GPU-sborka ne najdena (net ggml-cuda.dll / ggml-vulkan.dll). Udalyte runtime-cuda i runtime-vulkan, zapustite start.bat snova."
    }
} else {
    $exe = $cpu
    if (-not $exe) { $exe = $cuda }
    if (-not $exe) { $exe = $vulkan }
}

$code = Start-Llama $exe $choice.Ngl $choice.Name
exit $code
