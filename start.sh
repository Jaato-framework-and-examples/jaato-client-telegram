#!/bin/bash
# Quick start script for jaato-client-telegram

set -e

echo "🚀 jaato-client-telegram Quick Start"
echo ""

# Check if config exists
if [ ! -f "jaato-client-telegram.yaml" ]; then
    echo "📝 Creating config file from example..."
    cp config.example.yaml jaato-client-telegram.yaml
    echo "✅ Config file created: jaato-client-telegram.yaml"
    echo ""
    echo "⚠️  Please edit jaato-client-telegram.yaml and set:"
    echo "   - telegram.bot_token (get from @BotFather)"
    echo "   - jaato.socket_path (verify jaato server socket)"
    echo ""
    read -p "Press Enter after configuring..."
fi

# Check if bot token is set
TOKEN_LINE=$(grep 'bot_token:' jaato-client-telegram.yaml | grep -v '^#' | head -1)

# Check if token uses environment variable
if echo "$TOKEN_LINE" | grep -q '\${TELEGRAM_BOT_TOKEN}'; then
    if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
        echo "❌ ERROR: TELEGRAM_BOT_TOKEN environment variable not set"
        echo ""
        echo "Your config uses \${TELEGRAM_BOT_TOKEN} but the env var is not set."
        echo ""
        echo "Please set your bot token:"
        echo "  export TELEGRAM_BOT_TOKEN='your-bot-token-here'"
        exit 1
    fi
# Check if token is hardcoded (starts with a number like real bot tokens)
elif echo "$TOKEN_LINE" | grep -q 'bot_token:[[:space:]]*"[0-9]'; then
    echo "⚠️  WARNING: Bot token is hardcoded in jaato-client-telegram.yaml"
    echo ""
    echo "For better security, consider using environment variables instead:"
    echo "  1. Edit jaato-client-telegram.yaml and change bot_token to:"
    echo '     bot_token: "${TELEGRAM_BOT_TOKEN}"'
    echo "  2. Set the environment variable:"
    echo "     export TELEGRAM_BOT_TOKEN='your-bot-token-here'"
    echo ""
    echo "Continuing with hardcoded token..."
    echo ""
fi

# Check if jaato server is running
SOCKET_PATH=$(grep 'socket_path:' jaato-client-telegram.yaml | awk '{print $2}' | tr -d '"')
if [ ! -S "$SOCKET_PATH" ]; then
    echo "❌ ERROR: jaato server socket not found at $SOCKET_PATH"
    echo ""
    echo "Please ensure jaato server is running:"
    echo "  cd /path/to/jaato"
    echo "  python -m jaato"
    echo ""
    read -p "Press Enter to start anyway (will fail if server not running)..."
fi

# Install dependencies if needed
if ! python -c "import jaato_sdk" 2>/dev/null; then
    echo "📦 Installing dependencies..."
    pip install -e .
    echo ""
fi

# Start the bot
echo "▶️  Starting jaato-client-telegram..."
echo ""
python -m jaato_client_telegram
