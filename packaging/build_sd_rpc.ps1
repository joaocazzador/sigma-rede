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

# o rpc-server compilado com o ggml DO PRÓPRIO stable-diffusion.cpp: o do llama.cpp (sync-llama.last) tem menos
# operações (101 contra 105) e cai com "op not implemented" no Wan. Só o rpc-server.cpp vem do llama.cpp.
$rpcProj = Join-Path $Work "rpcsd"
New-Item -ItemType Directory -Force $rpcProj | Out-Null
@"
cmake_minimum_required(VERSION 3.14)
project(sd_rpc_server C CXX)
add_definitions(-DGGML_MAX_NAME=160)
set(GGML_RPC ON CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
add_subdirectory($($src -replace '\\','/')/ggml ggml)
add_executable(rpc-server $($llama -replace '\\','/')/tools/rpc/rpc-server.cpp)
target_link_libraries(rpc-server PRIVATE ggml)
target_compile_features(rpc-server PRIVATE cxx_std_17)
"@ | Set-Content -Encoding ascii (Join-Path $rpcProj "CMakeLists.txt")
& $cm -S $rpcProj -B (Join-Path $Work "build-rpcsd") -A x64
& $cm --build (Join-Path $Work "build-rpcsd") --config Release --target rpc-server -j 8

Copy-Item (Join-Path $Work "build-sd\bin\Release\sd-cli.exe") (Join-Path $Out "sd-cli.exe") -Force
Copy-Item (Join-Path $Work "build-rpcsd\Release\rpc-server.exe") (Join-Path $Out "rpc-server.exe") -Force
Get-ChildItem $Out | ForEach-Object { "{0}  {1}" -f (Get-FileHash $_.FullName).Hash.ToLower(), $_.Name }
