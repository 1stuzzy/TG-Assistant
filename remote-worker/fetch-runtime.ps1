param(
    [switch]$NeedGpu,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$headers = @{ "User-Agent" = "TG-Assistant-worker" }

function Get-Exe($dir) {
    if (-not (Test-Path $dir)) { return $null }
    return Get-ChildItem $dir -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Test-Nvidia {
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { return $true }
    foreach ($p in @(
        "$env:SystemRoot\System32\nvidia-smi.exe",
        "${env:ProgramFiles}\NVIDIA Corporation\NVSMI\nvidia-smi.exe"
    )) {
        if (Test-Path -LiteralPath $p) { return $true }
    }
    return $false
}

function Flatten-NextToExe($dir) {
    $exe = Get-Exe $dir
    if (-not $exe) { return }
    Get-ChildItem -LiteralPath $dir -Recurse -File -Include *.dll | ForEach-Object {
        $dest = Join-Path $exe.DirectoryName $_.Name
        if ($_.FullName -ne $dest) {
            Copy-Item -LiteralPath $_.FullName -Destination $dest -Force
        }
    }
}

function Save-Zip($asset, $dest) {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $zip = Join-Path $env:TEMP $asset.name
    Write-Host "Kachaju $($asset.name) ..."
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -Headers $headers
    Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
    Flatten-NextToExe $dest
}

$cpuDir = Join-Path $root "runtime-cpu"
$cudaDir = Join-Path $root "runtime-cuda"
$vkDir = Join-Path $root "runtime-vulkan"
$hasNvidia = Test-Nvidia
$wantGpu = $NeedGpu -or $hasNvidia

if (-not $Force -and (Get-Exe $cpuDir) -and ((-not $wantGpu) -or (Get-Exe $cudaDir) -or (Get-Exe $vkDir))) {
    Flatten-NextToExe $cpuDir
    Flatten-NextToExe $cudaDir
    Flatten-NextToExe $vkDir
    Write-Host "llama-server uzhe skachan"
    exit 0
}

Write-Host "Ischu sborku llama.cpp na GitHub..."
$releases = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=25" -Headers $headers
$cpu = $null
$cuda = $null
$cudart = $null
$vulkan = $null
foreach ($rel in $releases) {
    foreach ($asset in $rel.assets) {
        $n = $asset.name
        if (-not $cpu -and $n -like "*win-cpu-x64.zip") { $cpu = $asset }
        if (-not $cuda -and $n -like "*win-cuda-12.4-x64.zip") { $cuda = $asset }
        if (-not $cudart -and $n -like "cudart-llama-bin-win-cuda-12.4-x64.zip") { $cudart = $asset }
        if (-not $vulkan -and $n -like "*win-vulkan-x64.zip") { $vulkan = $asset }
    }
    if ($cpu) { break }
}
if (-not $cpu) { throw "Ne nashyol win-cpu-x64.zip" }

if ($Force -or -not (Get-Exe $cpuDir)) { Save-Zip $cpu $cpuDir }
if ($wantGpu -and $cuda -and ($Force -or -not (Get-Exe $cudaDir))) {
    Write-Host "Kachaju CUDA-sborku (nuzhna dlja GPU)"
    Save-Zip $cuda $cudaDir
    if ($cudart) { Save-Zip $cudart $cudaDir }
}
if ($wantGpu -and $vulkan -and ($Force -or -not (Get-Exe $vkDir))) {
    Write-Host "Kachaju Vulkan-sborku (zapasnoy GPU)"
    Save-Zip $vulkan $vkDir
}

Flatten-NextToExe $cpuDir
Flatten-NextToExe $cudaDir
Flatten-NextToExe $vkDir

if (-not (Get-Exe $cpuDir) -and -not (Get-Exe $cudaDir) -and -not (Get-Exe $vkDir)) {
    throw "llama-server.exe ne najden"
}
Write-Host "Gotovo."
