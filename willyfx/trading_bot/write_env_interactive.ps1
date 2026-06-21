# Interactive helper to create a local .env in the trading_bot folder.
# This script prompts for values and writes them to a `.env` file in the
# same directory. It does NOT commit anything to git.

Write-Host "This will create a .env file in: $PSScriptRoot" -ForegroundColor Cyan

$claude = Read-Host "CLAUDE_API_KEY"
$telegram = Read-Host "TELEGRAM_BOT_TOKEN"
$chat = Read-Host "TELEGRAM_CHAT_ID"

$envPath = Join-Path -Path $PSScriptRoot -ChildPath ".env"

$content = @"
CLAUDE_API_KEY=$claude
TELEGRAM_BOT_TOKEN=$telegram
TELEGRAM_CHAT_ID=$chat
"@

$content | Out-File -FilePath $envPath -Encoding utf8 -Force

Write-Host "Wrote .env to $envPath" -ForegroundColor Green
