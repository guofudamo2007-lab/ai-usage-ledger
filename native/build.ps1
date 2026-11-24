param([string]$Destination = (Join-Path $PSScriptRoot '..\release\rust'))
$ErrorActionPreference = 'Stop'
$target = 'x86_64-pc-windows-msvc'
Push-Location $PSScriptRoot
try {
    $dest = [IO.Path]::GetFullPath($Destination)
    & cargo build --locked --release --target $target
    if ($LASTEXITCODE) { throw 'Rust 编译失败' }
    $metadata = & cargo metadata --locked --no-deps --format-version 1 | ConvertFrom-Json
    if ($LASTEXITCODE) { throw '无法确定 Cargo 构建目录' }
    $source = Join-Path $metadata.target_directory "$target\release\kedu-native.exe"
    $null = New-Item -ItemType Directory -Force -Path $dest
    $exe = Join-Path $dest 'Kedu-Rust-Windows-x64.exe'
    Copy-Item -LiteralPath $source -Destination $exe -Force
    $digest = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText((Join-Path $dest 'SHA256SUMS.txt'), "$digest  Kedu-Rust-Windows-x64.exe" + [Environment]::NewLine, [Text.Encoding]::ASCII)
    Write-Output $exe
    Write-Output $digest
} finally {
    Pop-Location
}
