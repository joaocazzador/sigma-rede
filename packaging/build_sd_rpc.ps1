# Compila os dois programas do grupo de vídeo (docs/GRUPO-VIDEO.md), que o pacote oficial do stable-diffusion.cpp
# não traz: o sd-cli com RPC (coordenador) e o rpc-server compatível (trabalhador). Precisa de git e do Visual
# Studio Build Tools com C++ (o CMake vem junto). Saída: <Out>\sd-cli.exe e <Out>\rpc-server.exe
param(
    [string]$Work = "C:\sgtmp\sdrpc",
    [string]$Out = "C:\sgtmp\sdrpc\bin",
    [string]$SdBuild = "master-1026-997478a"   # o mesmo SD_BUILD de sigma/media.py
)
$ErrorActionPreference = "Stop"
$cm = Get-ChildItem "C:\Program Files (x86)\Microsoft Visual Studio" -Recurse -Filter cmake.exe -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $cm) { throw "CMake não encontrado (instale o Visual Studio Build Tools com C++)" }

New-Item -ItemType Directory -Force $Work, $Out | Out-Null
$src = Join-Path $Work "src"
if (-not (Test-Path $src)) { git clone --recursive https://github.com/leejet/stable-diffusion.cpp $src }
git -C $src checkout -q $SdBuild
git -C $src submodule update --init --recursive

$hash = (Get-Content (Join-Path $src "ggml\scripts\sync-llama.last")).Trim()
$llama = Join-Path $Work "llama"
if (-not (Test-Path $llama)) {
    New-Item -ItemType Directory -Force $llama | Out-Null
    git -C $llama init -q
    git -C $llama remote add origin https://github.com/ggml-org/llama.cpp.git
}
git -C $llama fetch -q --depth 1 origin $hash
git -C $llama checkout -q FETCH_HEAD

& $cm -S $src -B (Join-Path $Work "build-sd") -A x64 -DSD_RPC=ON -DSD_BUILD_SHARED_LIBS=OFF
& $cm --build (Join-Path $Work "build-sd") --config Release --target sd-cli -j 8

# GGML_MAX_NAME=160: o tamanho de nome de tensor do sd.cpp (sem isto a transferência falha); /EHsc /GR porque
# CMAKE_CXX_FLAGS substitui os padrões do MSVC
& $cm -S $llama -B (Join-Path $Work "build-rpc") -A x64 -DGGML_RPC=ON -DGGML_BUILD_SHARED_LIBS=OFF `
    -DBUILD_SHARED_LIBS=OFF -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF `
    "-DCMAKE_C_FLAGS=/DWIN32 /D_WINDOWS /DGGML_MAX_NAME=160" `
    "-DCMAKE_CXX_FLAGS=/DWIN32 /D_WINDOWS /EHsc /GR /DGGML_MAX_NAME=160"
& $cm --build (Join-Path $Work "build-rpc") --config Release --target ggml-rpc-server -j 8

Copy-Item (Join-Path $Work "build-sd\bin\Release\sd-cli.exe") (Join-Path $Out "sd-cli.exe") -Force
Copy-Item (Join-Path $Work "build-rpc\bin\Release\ggml-rpc-server.exe") (Join-Path $Out "rpc-server.exe") -Force
Get-ChildItem $Out | ForEach-Object { "{0}  {1}" -f (Get-FileHash $_.FullName).Hash.ToLower(), $_.Name }
