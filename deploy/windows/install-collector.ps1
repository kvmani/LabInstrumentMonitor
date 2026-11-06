<#
.SYNOPSIS
  Offline installer for the Lab Instrument Monitor collector on Windows 11 (run as Administrator).

.DESCRIPTION
  install-collector.ps1 -Bundle <dir> -WinSW <WinSW-x64.exe> [-Config <collector.toml> -Token <file>]
  install-collector.ps1 -Activate <version>          # code rollback / roll-forward

  * Never downloads: pip runs with --no-index --require-hashes from the bundle's wheelhouse-win,
    after every bundle checksum has been verified.
  * Code: %ProgramFiles%\labmon\releases\<version>; %ProgramFiles%\labmon\current is a junction
    switched while the service is stopped. State: %ProgramData%\labmon\collector.
  * The service runs as NT AUTHORITY\LocalService. The token is readable only by that account
    and Administrators (the Windows equivalent of mode 0600).
  Status: written against the documented WinSW v2 and Windows tooling, not yet run on Windows
  (ADR 0007). Rehearse on a representative Windows 11 VM before use.
#>
[CmdletBinding(DefaultParameterSetName = 'Install')]
param(
  [Parameter(ParameterSetName = 'Install', Mandatory)] [string] $Bundle,
  [Parameter(ParameterSetName = 'Install', Mandatory)] [string] $WinSW,
  [Parameter(ParameterSetName = 'Install')] [string] $Config,
  [Parameter(ParameterSetName = 'Install')] [string] $Token,
  [Parameter(ParameterSetName = 'Activate', Mandatory)] [string] $Activate
)
$ErrorActionPreference = 'Stop'
$Opt = Join-Path $env:ProgramFiles 'labmon'
$State = Join-Path $env:ProgramData 'labmon\collector'
$Service = 'labmon-collector'
$Account = 'NT AUTHORITY\LOCAL SERVICE'
function Log($m) { Write-Host "[install] $m" }

function Switch-Release([string] $Version) {
  $dest = Join-Path $Opt "releases\$Version"
  if (-not (Test-Path $dest) -or (Test-Path (Join-Path $dest '.installing'))) {
    throw "no complete release $Version in $Opt\releases"
  }
  if (Get-Service $Service -ErrorAction SilentlyContinue) { Stop-Service $Service }
  $current = Join-Path $Opt 'current'
  if (Test-Path $current) { cmd /c rmdir "$current" | Out-Null }   # removes the junction only
  New-Item -ItemType Junction -Path $current -Target $dest | Out-Null
  Log "active release: $Version"
  if (Get-Service $Service -ErrorAction SilentlyContinue) { Start-Service $Service; Log 'service started' }
}

if ($PSCmdlet.ParameterSetName -eq 'Activate') { Switch-Release $Activate; exit 0 }

Log 'verifying checksums'
foreach ($line in Get-Content (Join-Path $Bundle 'SHA256SUMS')) {
  $hash, $rel = $line -split '\s+', 2
  $path = Join-Path $Bundle ($rel.TrimStart('*').Substring(2))   # entries are "./path"
  if ((Get-FileHash -Algorithm SHA256 $path).Hash -ne $hash.ToUpper()) { throw "checksum mismatch: $rel" }
}
$release = Get-Content (Join-Path $Bundle 'RELEASE.json') | ConvertFrom-Json
$version = $release.version
$dest = Join-Path $Opt "releases\$version"
if (Test-Path (Join-Path $dest '.installing')) { Log "removing an interrupted install of $version"; Remove-Item -Recurse -Force $dest }
if (Test-Path $dest) {
  Log "release $version already installed"
} else {
  # virtualenvs embed absolute paths, so build in place behind a marker instead of renaming
  Log "installing $version into $dest"
  New-Item -ItemType Directory -Force $dest | Out-Null
  New-Item -ItemType File (Join-Path $dest '.installing') | Out-Null
  & py -3.12 -m venv (Join-Path $dest 'venv'); if ($LASTEXITCODE) { throw 'venv failed (is Python 3.12 installed?)' }
  $py = Join-Path $dest 'venv\Scripts\python.exe'
  $wh = Join-Path $Bundle 'wheelhouse-win'
  & $py -m pip install --quiet --no-index --find-links $wh --require-hashes -r (Join-Path $Bundle 'requirements.txt')
  if ($LASTEXITCODE) { throw 'dependency install failed' }
  & $py -m pip install --quiet --no-index --no-deps (Get-ChildItem (Join-Path $wh 'labmon-*.whl')).FullName
  if ($LASTEXITCODE) { throw 'labmon install failed' }
  Copy-Item (Join-Path $Bundle 'RELEASE.json'), (Join-Path $Bundle 'SBOM.json') $dest
  Copy-Item -Recurse (Join-Path $Bundle 'deploy') (Join-Path $dest 'deploy')   # for -Activate later
  Remove-Item (Join-Path $dest '.installing')
}

New-Item -ItemType Directory -Force $State, (Join-Path $State 'logs') | Out-Null
if ($Config) { Copy-Item $Config (Join-Path $State 'collector.toml') }
if ($Token) { Copy-Item $Token (Join-Path $State 'collector.token') }
# state: the service account may write (spool, logs); only it and Administrators may read the token
icacls $State /grant "${Account}:(OI)(CI)M" | Out-Null
$tok = Join-Path $State 'collector.token'
if (Test-Path $tok) {
  icacls $tok /inheritance:r /grant:r "${Account}:R" 'BUILTIN\Administrators:F' | Out-Null
}

$svcDir = Join-Path $Opt 'winsw'
New-Item -ItemType Directory -Force $svcDir | Out-Null
Copy-Item $WinSW (Join-Path $svcDir "$Service.exe")
Copy-Item (Join-Path $Bundle 'deploy\windows\labmon-collector.xml') (Join-Path $svcDir "$Service.xml")
if (-not (Get-Service $Service -ErrorAction SilentlyContinue)) {
  & (Join-Path $svcDir "$Service.exe") install; if ($LASTEXITCODE) { throw 'service install failed' }
  & sc.exe config $Service obj= 'NT AUTHORITY\LocalService' password= '' | Out-Null
}
Switch-Release $version
