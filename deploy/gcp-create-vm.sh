#!/usr/bin/env bash
# ============================================================
# Create a Google Cloud VM for AlgoBot
# Run this FROM YOUR LAPTOP (requires gcloud CLI installed)
#
# Usage:
#   chmod +x deploy/gcp-create-vm.sh
#   ./deploy/gcp-create-vm.sh              # e2-micro (free tier)
#   ./deploy/gcp-create-vm.sh e2-small     # $15/mo (more headroom)
# ============================================================
set -euo pipefail

VM_NAME="algobot"
ZONE="us-east1-b"            # Close to NYSE for low latency
MACHINE="${1:-e2-micro}"     # Default: free tier
IMAGE_FAMILY="ubuntu-2204-lts"
IMAGE_PROJECT="ubuntu-os-cloud"
DISK_SIZE="20GB"

echo "============================================"
echo "  AlgoBot - Google Cloud VM Setup"
echo "============================================"
echo "  VM Name:     $VM_NAME"
echo "  Machine:     $MACHINE"
echo "  Zone:        $ZONE"
echo "  OS:          Ubuntu 22.04 LTS"
echo "  Disk:        $DISK_SIZE"
echo ""

if [ "$MACHINE" = "e2-micro" ]; then
    echo "  Cost:        FREE (always-free tier)"
elif [ "$MACHINE" = "e2-small" ]; then
    echo "  Cost:        ~\$15/month"
else
    echo "  Cost:        varies"
fi
echo "============================================"
echo ""

# Check gcloud is installed
if ! command -v gcloud &> /dev/null; then
    echo "ERROR: gcloud CLI not installed."
    echo "Install it: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

# Check if logged in
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | head -1 | grep -q '@'; then
    echo "Not logged in to gcloud. Running: gcloud auth login"
    gcloud auth login
fi

# Show current project
PROJECT=$(gcloud config get-value project 2>/dev/null)
if [ -z "$PROJECT" ]; then
    echo "No GCP project set. List your projects:"
    gcloud projects list
    echo ""
    read -p "Enter your project ID: " PROJECT
    gcloud config set project "$PROJECT"
fi
echo "Using project: $PROJECT"
echo ""

# Create firewall rule for dashboard (port 5000)
echo "Creating firewall rule for dashboard (port 5000)..."
gcloud compute firewall-rules create allow-algobot-dashboard \
    --allow=tcp:5000 \
    --source-ranges=0.0.0.0/0 \
    --description="Allow AlgoBot dashboard access" \
    --target-tags=algobot \
    2>/dev/null || echo "  (firewall rule already exists - OK)"

# Create the VM
echo ""
echo "Creating VM '$VM_NAME'..."
gcloud compute instances create "$VM_NAME" \
    --machine-type="$MACHINE" \
    --zone="$ZONE" \
    --image-family="$IMAGE_FAMILY" \
    --image-project="$IMAGE_PROJECT" \
    --boot-disk-size="$DISK_SIZE" \
    --tags=algobot \
    --metadata=startup-script='#!/bin/bash
        apt-get update -qq
        apt-get install -y -qq python3-pip python3-venv git xvfb > /dev/null 2>&1
    '

echo ""
echo "============================================"
echo "  VM Created Successfully!"
echo "============================================"
echo ""
echo "Next steps:"
echo ""
echo "  1. SSH into your VM:"
echo "     gcloud compute ssh $VM_NAME --zone=$ZONE"
echo ""
echo "  2. Clone your repo:"
echo "     git clone <your-repo-url> ~/tpstrategyv3"
echo ""
echo "  3. Run the setup script:"
echo "     cd ~/tpstrategyv3"
echo "     chmod +x deploy/gcp-setup.sh"
echo "     ./deploy/gcp-setup.sh"
echo ""
echo "  4. Edit your .env file:"
echo "     nano ~/tpstrategyv3/.env"
echo ""
echo "  5. Start the bot:"
echo "     sudo systemctl start algobot"
echo ""
echo "  Dashboard will be at:"
EXTERNAL_IP=$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" --format='get(networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null || echo "PENDING")
echo "     http://$EXTERNAL_IP:5000"
echo ""
