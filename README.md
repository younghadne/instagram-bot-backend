# Instagram Bot Web App

A web-based Instagram automation bot with multiple features including auto-follow, auto-unfollow, auto-like, mass story viewing, and DM automation.

## Features

- **Auto Follow** — Follow followers of target accounts
- **Auto Unfollow** — Unfollow users from your following list
- **Auto Like Feed** — Like posts in your home feed
- **Mass Story View** — View stories from your feed
- **Auto DM** — Send direct messages to users
- **Welcome DM** — Send welcome messages to new followers
- **Auto Approve Requests** — Approve pending follow requests
- **Auto Comment** — Leave comments on posts

## Installation

```bash
# Install dependencies
pip install -r requirements-web.txt
```

## Running Locally

```bash
python web_app.py
```

Then open `http://127.0.0.1:5000` in your browser.

## Login Methods

1. **Browser Login** — Opens Chrome, you login yourself, bot captures session (recommended)
2. **Password Login** — Enter username/password directly

Sessions are automatically saved and loaded on subsequent visits.

## Deployment

### Cloudflare Workers

1. Install Wrangler CLI:
```bash
npm install -g wrangler
```

2. Login to Cloudflare:
```bash
wrangler login
```

3. Deploy:
```bash
wrangler deploy
```

## Security

- Sessions are saved locally in `sessions/` directory
- Never share your session files
- Use 2FA for your Instagram account
- The bot uses safe delays (3-6 seconds) to avoid detection

## Anti-Detection

- Random delays between actions
- Breaks every 15 follows
- Session persistence to avoid repeated logins
- User-agent spoofing for browser login

## License

MIT
