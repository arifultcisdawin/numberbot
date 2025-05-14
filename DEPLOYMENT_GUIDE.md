# Telegram Twilio Bot Deployment Guide

This document provides guidance on how to deploy and run the Telegram Twilio Bot 24/7.

## Prerequisites

Before deploying, make sure you have:

1. Telegram Bot Token (from BotFather)
2. MongoDB database setup (local or cloud-based)
3. Basic knowledge of your chosen hosting platform

## Deployment Options

### Option 1: Railway.app (Recommended)

Railway is a modern platform that makes deployment very straightforward.

1. Sign up at [Railway.app](https://railway.app/)
2. Create a new project
3. Connect your GitHub repository or use the Railway CLI to deploy your code
4. Set up environment variables:
   - TELEGRAM_BOT_TOKEN
   - MONGODB_URI
5. Deploy your bot with the command: `python main.py`

Benefits: Free tier available, easy to use, auto-deploys.

### Option 2: Render.com

Render is another great platform with a generous free tier.

1. Sign up at [Render.com](https://render.com/)
2. Create a new Web Service
3. Connect your GitHub repository
4. Set environment variables
5. Choose a plan (even the free tier works for the bot)
6. Deploy with the command: `python main.py`

Benefits: Reliable, good free tier, easy deployment.

### Option 3: Fly.io

Fly.io offers global deployment options:

1. Install Fly CLI: `curl -L https://fly.io/install.sh | sh`
2. Sign up: `flyctl auth signup`
3. Create a new app: `flyctl apps create`
4. Create a `Dockerfile` in your project:
   ```dockerfile
   FROM python:3.9-slim
   
   WORKDIR /app
   
   COPY requirements.txt .
   RUN pip install --no-cache-dir -r requirements.txt
   
   COPY . .
   
   CMD ["python", "main.py"]
   ```
5. Create a `fly.toml` file:
   ```toml
   app = "your-bot-name"
   
   [env]
     TELEGRAM_BOT_TOKEN = "your_token"
     MONGODB_URI = "your_mongodb_uri"
   
   [processes]
   app = "python main.py"
   ```
6. Deploy: `flyctl deploy`

Benefits: Global edge deployment, good free tier.

### Option 4: VPS (DigitalOcean, Linode, AWS EC2)

For more control, use a VPS:

1. Create a server (Ubuntu recommended)
2. SSH into your server
3. Install dependencies:
   ```bash
   apt update
   apt install -y python3-pip python3-venv git
   ```
4. Clone your repository:
   ```bash
   git clone https://github.com/yourusername/telegram-twilio-bot.git
   cd telegram-twilio-bot
   ```
5. Create a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
6. Create a systemd service for 24/7 operation:
   ```bash
   sudo nano /etc/systemd/system/telegram-bot.service
   ```
   Add the following:
   ```
   [Unit]
   Description=Telegram Twilio Bot
   After=network.target
   
   [Service]
   User=yourusername
   WorkingDirectory=/home/yourusername/telegram-twilio-bot
   ExecStart=/home/yourusername/telegram-twilio-bot/venv/bin/python main.py
   Restart=always
   
   [Install]
   WantedBy=multi-user.target
   ```
7. Enable and start the service:
   ```bash
   sudo systemctl enable telegram-bot
   sudo systemctl start telegram-bot
   ```

Benefits: Full control, can be cost-effective for long-term.

## Monitoring and Maintenance

1. Set up logging to a file or external service like Sentry
2. Create regular backups of your MongoDB database
3. Set up monitoring alerts for downtime
4. Check Twilio credentials regularly

## Important Notes

1. Make sure your MongoDB instance is properly secured
2. Keep your .env file secure and never commit it to public repositories
3. Use the correct Twilio API version in your imports
4. Consider setting up automatic restarts in case of crashes
5. Regularly check logs for errors or unexpected behavior

## Troubleshooting

If your bot stops working:
1. Check logs for errors
2. Verify all environment variables are set correctly
3. Ensure MongoDB connection is working
4. Test Twilio API credentials
5. Check for Telegram API or network issues