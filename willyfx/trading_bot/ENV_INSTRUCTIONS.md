# Environment setup (DO NOT commit secrets)

This project uses a `.env` file to store sensitive credentials (MT5 login, Telegram token, API keys).
Do NOT commit a file containing real secrets into the repository. Keep a local `.env` in either the repository root or the `trading_bot` folder.

Example PowerShell command to create a local `.env` in `previous bot/trading_bot` (edit values locally):

```powershell
$envContent = @"
MT5_LOGIN=YOUR_MT5_LOGIN
MT5_PASSWORD=YOUR_MT5_PASSWORD
MT5_SERVER=YOUR_MT5_SERVER

TELEGRAM_SIGNAL_BOT_ENABLED=true
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_TELEGRAM_CHAT_ID

NEWS_API_KEY=YOUR_NEWS_API_KEY
NEWS_ANALYSIS_INTERVAL_MINS=30
NEWS_QUERY=XAUUSD OR gold OR bullion OR "gold price" OR "Federal Reserve" OR FOMC OR CPI OR inflation OR yields OR "US dollar" OR DXY OR "Treasury yields"

CLAUDE_API_KEY=YOUR_CLAUDE_API_KEY
"@

$envPath = Join-Path -Path (Split-Path -Parent $MyInvocation.MyCommand.Path) ".env"
$envContent | Out-File -FilePath $envPath -Encoding utf8

Write-Host "Created local .env at $envPath (verify values and keep it private)"
```

If you prefer a one-liner that prompts for sensitive values interactively (PowerShell):

```powershell
Read-Host "MT5_LOGIN" | Out-File .env -Encoding utf8
Read-Host "MT5_PASSWORD" -AsSecureString | ConvertFrom-SecureString | Out-File -Append .env -Encoding utf8
# then append other variables similarly
```

Windows tip: mark the `.env` file as hidden and add it to your global `.gitignore` if you accidentally committed earlier.

If you want, I can create a non-committed helper script to walk you through creating the `.env` locally — say the word and I'll add it.
