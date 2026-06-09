# Railway Deployment Setup

## Step 1 — Create Railway project

1. Go to railway.app → New Project → Deploy from GitHub repo
2. Connect this repo
3. Railway will auto-detect Python and use the Procfile

## Step 2 — Set environment variables in Railway dashboard

Go to your service → Variables tab → Add all of these:

### Sell.do
```
SELL_DO_API_KEY=22c58f46043f2d70474f2314ca72faa7
SELL_DO_NOTE_API_KEY=880fd9ccb71d8b6b8f15b19f7f092936
```

### Microsoft Graph (SharePoint)
```
GRAPH_TENANT_ID=d0f025f6-6cda-470f-be0c-2f50c564a639
GRAPH_CLIENT_ID=47e5dc4d-826f-4a25-a00f-988ed032661a
GRAPH_THUMB=E75578C7AA2C5CE54C326D9DD9F96D0E0DFC9698
GRAPH_PEM_CONTENT=<paste full PEM key content — see Step 3>
```

### WasenderAPI
```
WASENDER_API_KEY=<your wasender api key>
WASENDER_SESSION_ID=<your wasender session id>
SALES_LINE_PHONE=919840097140
```

### Sarvam
```
SARVAM_KEY=<your sarvam api key>
```

### Anthropic
```
ANTHROPIC_API_KEY=<your anthropic api key>
```

## Step 3 — Get the PEM key content

Run this in PowerShell on your laptop:

```powershell
Get-Content "C:\Users\bharathimeraki\Downloads\PinnacleLeadPoller_key.pem" -Raw
```

Copy the entire output (including the -----BEGIN RSA PRIVATE KEY----- and -----END RSA PRIVATE KEY----- lines).
Paste it as the value of GRAPH_PEM_CONTENT in Railway.

## Step 4 — Deploy

Railway auto-deploys when you push to GitHub.
Or click "Deploy" manually in the Railway dashboard.

## Step 5 — Set WasenderAPI inbound webhook

After deployment, Railway gives you a public URL like:
`https://pinnacle-lead-nurturing-production.up.railway.app`

Go to WasenderAPI dashboard → your session → Webhook settings:
Set inbound webhook URL to:
`https://pinnacle-lead-nurturing-production.up.railway.app/webhook/inbound`

## Step 6 — Verify

Check Railway logs — you should see:
```
Poller thread started
Starting Flask webhook on port XXXX
── Poll cycle starting ──
Loading contacts from SharePoint...
Graph API token acquired
...
── Cycle done — Queued: X ...
```

And the health check:
`https://your-railway-url.up.railway.app/health` → {"status": "ok"}
