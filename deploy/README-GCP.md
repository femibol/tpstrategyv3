# Deploy AlgoBot on Google Cloud (Free Tier)

Your bot runs 24/7 on a Google Cloud VM — even when your laptop is off.

**Cost: $0/month** on the always-free `e2-micro` tier.

---

## Quick Start (5 minutes)

### Step 1: Create the VM (from your laptop)

Install the [gcloud CLI](https://cloud.google.com/sdk/docs/install) if you don't have it, then:

```bash
# Login to Google Cloud
gcloud auth login

# Set your project (create one at https://console.cloud.google.com if needed)
gcloud config set project YOUR_PROJECT_ID

# Create the VM (free tier)
chmod +x deploy/gcp-create-vm.sh
./deploy/gcp-create-vm.sh
```

Want more power? Pass `e2-small` (~$15/mo):
```bash
./deploy/gcp-create-vm.sh e2-small
```

### Step 2: SSH into your VM

```bash
gcloud compute ssh algobot --zone=us-east1-b
```

### Step 3: Clone your repo on the VM

```bash
git clone https://github.com/YOUR_USERNAME/tpstrategyv3.git ~/tpstrategyv3
cd ~/tpstrategyv3
```

### Step 4: Run the setup script

```bash
chmod +x deploy/gcp-setup.sh
./deploy/gcp-setup.sh
```

This installs Python, creates a venv, installs dependencies, and sets up systemd services.

### Step 5: Add your API keys

```bash
nano ~/tpstrategyv3/.env
```

Fill in at minimum:
- `POLYGON_API_KEY` (for market data scanning)
- `DISCORD_WEBHOOK_URL` (for trade alerts on your phone)
- `IBKR_HOST=127.0.0.1` (if running IB Gateway on same VM)

### Step 6: Start the bot

```bash
sudo systemctl start algobot
```

That's it. The bot survives reboots automatically.

---

## Daily Operations

| Command | What it does |
|---------|-------------|
| `sudo systemctl start algobot` | Start the bot |
| `sudo systemctl stop algobot` | Stop the bot |
| `sudo systemctl restart algobot` | Restart the bot |
| `sudo systemctl status algobot` | Check if running |
| `sudo journalctl -u algobot -f` | Watch live logs |
| `sudo journalctl -u algobot --since "1 hour ago"` | Recent logs |

---

## Dashboard Access

Your dashboard is at: `http://YOUR_VM_IP:5000`

Find your VM's IP:
```bash
gcloud compute instances describe algobot --zone=us-east1-b --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

---

## IB Gateway Setup (Optional)

If you trade through Interactive Brokers, you need IB Gateway running on the VM:

1. Download IB Gateway from [IBKR website](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php)

2. Upload to your VM:
   ```bash
   gcloud compute scp ~/Downloads/ibgateway-stable-standalone-linux-x64.sh algobot:~/
   ```

3. SSH in and install:
   ```bash
   gcloud compute ssh algobot --zone=us-east1-b
   chmod +x ~/ibgateway-stable-standalone-linux-x64.sh
   sudo ~/ibgateway-stable-standalone-linux-x64.sh
   ```

4. Start IB Gateway:
   ```bash
   sudo systemctl start ibgateway
   ```

**Note:** IB Gateway still requires you to log in periodically (IBKR security). Consider using [IBC](https://github.com/IbcAlpha/IBC) for automated restarts.

---

## Updating the Bot

```bash
gcloud compute ssh algobot --zone=us-east1-b
cd ~/tpstrategyv3
git pull
source venv/bin/activate
pip install -r requirements.txt -q
sudo systemctl restart algobot
```

---

## Cost Breakdown

| Resource | Free Tier | Paid |
|----------|-----------|------|
| e2-micro VM | **Free forever** (1 per project) | - |
| e2-small VM | - | ~$15/mo |
| 20GB disk | **Free** (30GB included) | - |
| Network egress | **Free** (1GB/mo) | $0.12/GB |
| **Total (free tier)** | **$0/month** | |

Your $300 credits cover 90 days of any usage. After that, e2-micro stays free.

---

## Troubleshooting

**Bot won't start:**
```bash
sudo journalctl -u algobot --since "5 minutes ago"
```

**Check .env is loaded:**
```bash
sudo systemctl show algobot | grep EnvironmentFile
```

**VM ran out of memory (e2-micro = 1GB RAM):**
```bash
# Add swap space
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo systemctl restart algobot
```

**Upgrade to e2-small if needed:**
```bash
# From your laptop
gcloud compute instances stop algobot --zone=us-east1-b
gcloud compute instances set-machine-type algobot --zone=us-east1-b --machine-type=e2-small
gcloud compute instances start algobot --zone=us-east1-b
```
