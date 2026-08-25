# Official llama-server.exe. Venv cannot be shipped — paths break and AVX wheels crash.
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$headers = @{ "User-Agent" = "TG-Assistant-worker" }

function Get-Exe($dir) {
    if (-not (Test-Path $dir)) { return $null }
    return Get-ChildItem $dir -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Save-Zip($asset, $dest) {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $zip = Join-Path $env:TEMP $asset.name
    Write-Host "Kachaju $($asset.name) ..."
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -Headers $headers
    Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
}

$cpuDir = Join-Path $root "runtime-cpu"
$cudaDir = Join-Path $root "runtime-cuda"
$hasNvidia = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)

if ((Get-Exe $cpuDir) -and ((-not $hasNvidia) -or (Get-Exe $cudaDir))) {
    Write-Host "llama-server uzhe skachan"
    exit 0
}

Write-Host "Ischu sborku llama.cpp na GitHub..."
$releases = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=25" -Headers $headers
$cpu = $null
$cuda = $null
$cudart = $null
foreach ($rel in $releases) {
    foreach ($asset in $rel.assets) {
        $n = $asset.name
        if (-not $cpu -and $n -like "*win-cpu-x64.zip") { $cpu = $asset }
        if (-not $cuda -and $n -like "*win-cuda-12.4-x64.zip") { $cuda = $asset }
        if (-not $cudart -and $n -like "cudart-llama-bin-win-cuda-12.4-x64.zip") { $cudart = $asset }
    }
    if ($cpu) { break }
}
if (-not $cpu) { throw "Ne nashyol win-cpu-x64.zip" }

if (-not (Get-Exe $cpuDir)) { Save-Zip $cpu $cpuDir }
if ($hasNvidia -and $cuda -and -not (Get-Exe $cudaDir)) {
    Write-Host "Nvidia najdena, kachaju CUDA"
    Save-Zip $cuda $cudaDir
    if ($cudart) { Save-Zip $cudart $cudaDir }
}

if (-not (Get-Exe $cpuDir) -and -not (Get-Exe $cudaDir)) { throw "llama-server.exe ne najden" }
Write-Host "Gotovo."
