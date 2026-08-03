[CmdletBinding()]
param(
  [ValidateRange(1, 65535)]
  [int]$ApiPort = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Import-Stage3Environment {
  param([Parameter(Mandatory)][string]$EnvironmentFile)

  if (-not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) {
    throw "Missing $EnvironmentFile. Copy .env.example to .env and configure DeepSeek first."
  }

  $allowedNames = @(
    "DEEPSEEK_MODEL",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_API_KEY",
    "API_SESSION_STORE_PATH",
    "API_CHECKPOINT_PATH",
    "API_KNOWLEDGE_DB_PATH",
    "API_VECTOR_DB_PATH",
    "API_KNOWLEDGE_EMBEDDING",
    "NEXT_PUBLIC_API_BASE_URL"
  )

  foreach ($rawLine in Get-Content -LiteralPath $EnvironmentFile -Encoding utf8) {
    $line = $rawLine.Trim()
    if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
      continue
    }

    $separator = $line.IndexOf("=")
    $name = $line.Substring(0, $separator).Trim()
    if ($allowedNames -notcontains $name) {
      continue
    }

    $currentValue = [Environment]::GetEnvironmentVariable($name, "Process")
    if ([string]::IsNullOrWhiteSpace($currentValue)) {
      $value = $line.Substring($separator + 1).Trim().Trim('"').Trim("'")
      Set-Item -LiteralPath "Env:$name" -Value $value
    }
  }
}

function Assert-RequiredEnvironment {
  foreach ($name in @("DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY")) {
    $value = [Environment]::GetEnvironmentVariable($name, "Process")
    if ([string]::IsNullOrWhiteSpace($value) -or $value -eq "replace-with-your-api-key") {
      throw "Missing $name. Configure the root .env before starting the Stage 3 stack."
    }
  }
}

function Wait-ForHttpOk {
  param(
    [Parameter(Mandatory)][System.Diagnostics.Process]$Process,
    [Parameter(Mandatory)][string]$Uri,
    [int]$TimeoutSeconds = 30
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    $Process.Refresh()
    if ($Process.HasExited) {
      throw "Service exited before it became ready: $Uri"
    }

    try {
      $response = Invoke-WebRequest -Uri $Uri -TimeoutSec 2 -UseBasicParsing
      if ($response.StatusCode -eq 200) {
        return
      }
    } catch {
      Start-Sleep -Milliseconds 250
    }
  }

  throw "Timed out waiting for $Uri"
}

function Stop-ManagedProcess {
  param([System.Diagnostics.Process]$Process)

  if ($null -eq $Process) {
    return
  }

  $Process.Refresh()
  if (-not $Process.HasExited) {
    Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
  }
}

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendRoot = Join-Path $repositoryRoot "backend"
$frontendRoot = Join-Path $repositoryRoot "frontend"
$pythonPath = Join-Path $backendRoot ".venv\\Scripts\\python.exe"
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
$nextCliPath = Join-Path $frontendRoot "node_modules\\next\\dist\\bin\\next"
$frontendPort = 3000

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
  throw "Missing $pythonPath. Run 'uv sync --extra dev' in backend first."
}
if ($null -eq $nodeCommand) {
  throw "node.exe was not found. Install a supported Node.js version first."
}
if (-not (Test-Path -LiteralPath (Join-Path $frontendRoot "node_modules") -PathType Container)) {
  throw "Missing frontend/node_modules. Run 'npm install' in frontend first."
}
if (-not (Test-Path -LiteralPath $nextCliPath -PathType Leaf)) {
  throw "Missing $nextCliPath. Run 'npm install' in frontend first."
}

$managedEnvironmentNames = @(
  "DEEPSEEK_MODEL",
  "DEEPSEEK_BASE_URL",
  "DEEPSEEK_API_KEY",
  "API_SESSION_STORE_PATH",
  "API_CHECKPOINT_PATH",
  "API_KNOWLEDGE_DB_PATH",
  "API_VECTOR_DB_PATH",
  "API_KNOWLEDGE_EMBEDDING",
  "NEXT_PUBLIC_API_BASE_URL",
  "PYTHONPATH"
)
$originalEnvironment = @{}
foreach ($name in $managedEnvironmentNames) {
  $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
$apiProcess = $null
$frontendProcess = $null
try {
  Import-Stage3Environment -EnvironmentFile (Join-Path $repositoryRoot ".env")
  Assert-RequiredEnvironment

  $env:PYTHONPATH = Join-Path $backendRoot "src"
  $runtimeDataDirectory = Join-Path $repositoryRoot "data"
  if (-not (Test-Path -LiteralPath $runtimeDataDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $runtimeDataDirectory | Out-Null
  }
  if ([string]::IsNullOrWhiteSpace($env:API_SESSION_STORE_PATH)) {
    $env:API_SESSION_STORE_PATH = Join-Path $runtimeDataDirectory "api_sessions.sqlite3"
  }
  if ([string]::IsNullOrWhiteSpace($env:API_CHECKPOINT_PATH)) {
    $env:API_CHECKPOINT_PATH = Join-Path $runtimeDataDirectory "api_checkpoints.sqlite3"
  }
  if ([string]::IsNullOrWhiteSpace($env:NEXT_PUBLIC_API_BASE_URL)) {
    $env:NEXT_PUBLIC_API_BASE_URL = "http://127.0.0.1:$ApiPort"
  }

  $apiStartInfo = @{
    FilePath = $pythonPath
    ArgumentList = @("-m", "uvicorn", "api.app:create_app", "--factory", "--host", "127.0.0.1", "--port", $ApiPort.ToString())
    WorkingDirectory = $backendRoot
    PassThru = $true
  }
  $apiProcess = Start-Process @apiStartInfo -WindowStyle Hidden

  # 清理列表与 $managedEnvironmentNames 的 API 专用变量保持同步：
  # 这些 env 只应存在于 API 子进程，不能泄漏给前端子进程（工作单 T2
  # 新增三个知识库变量）。
  foreach ($name in @("DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY", "API_SESSION_STORE_PATH", "API_CHECKPOINT_PATH", "API_KNOWLEDGE_DB_PATH", "API_VECTOR_DB_PATH", "API_KNOWLEDGE_EMBEDDING", "PYTHONPATH")) {
    Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
  }
  $frontendStartInfo = @{
    FilePath = $nodeCommand.Source
    ArgumentList = @($nextCliPath, "dev", "--hostname", "127.0.0.1", "--port", $frontendPort.ToString())
    WorkingDirectory = $frontendRoot
    PassThru = $true
  }
  $frontendProcess = Start-Process @frontendStartInfo -WindowStyle Hidden

  Wait-ForHttpOk -Process $apiProcess -Uri "http://127.0.0.1:$ApiPort/healthz"
  Wait-ForHttpOk -Process $frontendProcess -Uri "http://127.0.0.1:$frontendPort"

  Write-Host "Stage 3 services are ready:"
  Write-Host "  Frontend: http://127.0.0.1:$frontendPort"
  Write-Host "  API:      http://127.0.0.1:$ApiPort/docs"
  Write-Host "Press Ctrl+C to stop both services."

  while ($true) {
    Start-Sleep -Seconds 1
    $apiProcess.Refresh()
    $frontendProcess.Refresh()
    if ($apiProcess.HasExited -or $frontendProcess.HasExited) {
      throw "A Stage 3 service exited unexpectedly."
    }
  }
} finally {
  Stop-ManagedProcess -Process $frontendProcess
  Stop-ManagedProcess -Process $apiProcess
  foreach ($name in $managedEnvironmentNames) {
    $originalValue = $originalEnvironment[$name]
    if ($null -eq $originalValue) {
      Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    } else {
      Set-Item -LiteralPath "Env:$name" -Value $originalValue
    }
  }
}
