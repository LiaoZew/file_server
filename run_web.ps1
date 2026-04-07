$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$deps = Join-Path $root ".deps"
if (!(Test-Path $deps)) {
    Write-Host ".deps not found, installing dependencies from Tsinghua mirror..."
    $env:PIP_NO_INDEX = "0"
    $env:HTTP_PROXY = ""
    $env:HTTPS_PROXY = ""
    $env:ALL_PROXY = ""
    $env:http_proxy = ""
    $env:https_proxy = ""
    $env:all_proxy = ""
    pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r (Join-Path $root "requirements.txt") --target $deps
}
$env:PYTHONPATH = $deps
python (Join-Path $root "web_server.py")
