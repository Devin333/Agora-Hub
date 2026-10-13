[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RemainingArgs
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    [Console]::Error.WriteLine('项目 .venv 解释器不存在。请先按项目说明准备依赖；本工具不会回退到系统 Python。')
    exit 2
}

Set-Location -LiteralPath $ProjectRoot
& $Python -m scripts.local_desktop @RemainingArgs
exit $LASTEXITCODE
