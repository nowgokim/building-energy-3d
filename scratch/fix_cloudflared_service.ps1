[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$exe = 'C:\Users\User\AppData\Local\cloudflared\cloudflared.exe'
$cfg = 'C:\Users\User\.cloudflared\config.yml'

Write-Host "Force killing cloudflared process..."
taskkill /F /IM cloudflared.exe 2>$null
Start-Sleep -Seconds 2

Write-Host "Updating service binPath..."
$binPath = "`"$exe`" tunnel --config `"$cfg`" run"
sc.exe config Cloudflared binPath= $binPath
Start-Sleep -Seconds 1

Write-Host "Verifying service path..."
$path = (Get-WmiObject Win32_Service -Filter 'Name="Cloudflared"').PathName
Write-Host "Service path: $path"

Write-Host "Starting Cloudflared service..."
Start-Service -Name Cloudflared
Start-Sleep -Seconds 3

$svc = Get-Service -Name Cloudflared
Write-Host "Service status: $($svc.Status)"
