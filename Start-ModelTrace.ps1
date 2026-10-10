$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host 'Creating the local Python environment...'
    py -3 -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}

& $python -c 'import flask, click, numpy'
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing ModelTrace dependencies...'
    & $python -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Configured package mirror failed; retrying from PyPI...'
        & $python -m pip install --index-url https://pypi.org/simple -r (Join-Path $PSScriptRoot 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Could not install ModelTrace dependencies.' }
    }
}

Write-Host 'Opening ModelTrace at http://127.0.0.1:7860/'
Write-Host 'Keep this window open while testing. Press Ctrl+C to stop.'
& $python (Join-Path $PSScriptRoot 'start.py')
