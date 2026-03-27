#!/bin/bash
set -e

echo "Setting up Cornerstone MCP..."

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create .env if it doesn't exist
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "Created .env file. Edit it with your API URL and key:"
  echo "  nano .env"
fi

echo ""
echo "Setup complete. To configure Claude Code, add this to your Claude settings:"
echo ""
echo '  "mcpServers": {'
echo '    "cornerstone": {'
echo '      "command": "'"$(pwd)/.venv/bin/python"'",'
echo '      "args": ["'"$(pwd)/server.py"'", "--transport", "stdio"]'
echo '    }'
echo '  }'
echo ""
