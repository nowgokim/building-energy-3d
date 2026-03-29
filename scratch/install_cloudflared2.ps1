# cloudflared.exe 직접 다운로드 (MSI 대신)
$dest = "C:\Users\User\AppData\Local\cloudflared"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

$exePath = "$dest\cloudflared.exe"
$url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

Write-Host "Downloading cloudflared.exe..."
Invoke-WebRequest -Uri $url -OutFile $exePath -UseBasicParsing

Write-Host "Verifying..."
& $exePath --version

# PATH에 영구 추가
$currentPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
if ($currentPath -notlike "*$dest*") {
    [System.Environment]::SetEnvironmentVariable("PATH", "$currentPath;$dest", "User")
    Write-Host "PATH updated: $dest added"
}

Write-Host "cloudflared installed at: $exePath"
