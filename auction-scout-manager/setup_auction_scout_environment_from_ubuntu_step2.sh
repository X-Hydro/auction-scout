#!/bin/bash
set -e
set -x

# Define directories
USER_HOME="/home/oncoordadmin"
DOCKER_DIR="$USER_HOME/docker/auction-scout"
IMG_DIR="$DOCKER_DIR/images"

# Confirm the image tar exists before touching the running service
if [ ! -f "$IMG_DIR/auction-scout-manager.tar" ]; then
  echo "❌ File not found: $IMG_DIR/auction-scout-manager.tar"
  exit 1
fi

# Stop the existing service before loading the new image
echo "Stopping existing AuctionScout Manager service..."
cd "$DOCKER_DIR"
docker-compose stop auction-scout-manager
docker image prune -f

echo "Loading AuctionScout Manager Docker image..."
docker load -i "$IMG_DIR/auction-scout-manager.tar"
docker-compose rm -f auction-scout-manager


echo "setup_auction_scout_environment_from_ubuntu_step2.sh complete."
echo running: docker-compose --env-file ./.env up -d auction-scout-manager
docker-compose --env-file ./.env up -d auction-scout-manager

# Give the container a moment to either come up cleanly or crash (e.g. the
# placeholder-resolution failure we hit when ADMIN_SECRET_TOKEN was missing)
# before checking its logs.
echo "Waiting for auction-scout-manager to start..."
sleep 10

echo "----- auction-scout-manager: last 50 log lines -----"
docker-compose logs --tail=50 auction-scout-manager
echo "----- auction-scout-manager: container status -----"
docker-compose ps auction-scout-manager