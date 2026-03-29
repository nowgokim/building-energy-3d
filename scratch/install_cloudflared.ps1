$url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.msi"
$dest = "C:\Users\User\AppData\Local\Temp\cloudflared.msi"
Write-Host "Downloading cloudflared to $dest ..."
Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
Write-Host "Installing..."
Start-Process msiexec.exe -ArgumentList "/i `"$dest`" /quiet /norestart" -Wait
Write-Host "Install complete. Verifying..."
cloudflared --version
