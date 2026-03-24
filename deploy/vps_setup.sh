#!/bin/bash
# Run once on a fresh Ubuntu 22.04 VPS to set up the algo.
# Assumes you have already uploaded the project files to /home/ubuntu/SimpleAlgo

set -e

echo "── Installing system deps ──"
sudo apt-get update -y
sudo apt-get install -y python3-pip python3-venv

echo "── Creating virtualenv ──"
cd /home/ubuntu/SimpleAlgo
python3 -m venv venv
source venv/bin/activate

echo "── Installing Python deps ──"
pip install --upgrade pip
pip install -r requirements.txt

echo "── Copying systemd service ──"
sudo cp deploy/simplealgo.service /etc/systemd/system/simplealgo.service
sudo systemctl daemon-reload
sudo systemctl enable simplealgo.service

echo ""
echo "Setup complete."
echo "Next steps:"
echo "  1. Copy your .env file:   nano /home/ubuntu/SimpleAlgo/.env"
echo "     !! Set KITE_AUTH_SECRET_KEY to a random 32-char string !!"
echo "  2. Start the service:     sudo systemctl start simplealgo"
echo "  3. View logs:             sudo journalctl -u simplealgo -f"
echo "  4. Log in to Kite daily:  http://<your-vps-ip>:8880/kite/login"
