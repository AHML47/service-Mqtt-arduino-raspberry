#!/bin/bash

set -e

echo "Building Docker image..."
docker build -t service-builder .

echo "Creating temporary container..."
CID=$(docker create service-builder)

echo "Extracting binary..."
docker cp $CID:/app/build/service.dist/service.bin ./service.bin

echo "Cleaning up..."
docker rm $CID

echo "Done: service.bin created"