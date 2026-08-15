#!/usr/bin/env bash
# ================================================
# setup_ec2_swap.sh
# Configures a 2GB swap file on a fresh Ubuntu/Debian EC2 free-tier
# instance (t2.micro / t3.micro, 1GB RAM) so embedding batches and
# Chroma don't trigger an OOM kill under memory pressure.
# Run once, as root or with sudo: sudo bash setup_ec2_swap.sh
# ================================================
set -euo pipefail

SWAP_FILE="/swapfile"
SWAP_SIZE="2G"

if swapon --show | grep -q "$SWAP_FILE"; then
    echo "Swap file already active at $SWAP_FILE — nothing to do."
    exit 0
fi

echo "Allocating ${SWAP_SIZE} swap file at ${SWAP_FILE}..."
fallocate -l "$SWAP_SIZE" "$SWAP_FILE" || dd if=/dev/zero of="$SWAP_FILE" bs=1M count=2048
chmod 600 "$SWAP_FILE"
mkswap "$SWAP_FILE"
swapon "$SWAP_FILE"

if ! grep -q "$SWAP_FILE" /etc/fstab; then
    echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
    echo "Added swap entry to /etc/fstab (persists across reboots)."
fi

# Conservative swappiness — prefer RAM, only spill to swap under real pressure.
sysctl -w vm.swappiness=10
if ! grep -q "vm.swappiness" /etc/sysctl.conf; then
    echo "vm.swappiness=10" >> /etc/sysctl.conf
fi

echo "Done. Current swap status:"
swapon --show
free -h
