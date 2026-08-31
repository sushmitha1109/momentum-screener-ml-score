#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="${SCRIPT_DIR}/version.txt"

IMAGE_NAME="sushmitha2211/momentum-ml-score"

# Init version file
if [ ! -f "$VERSION_FILE" ]; then
    echo "1.0.0" > "$VERSION_FILE"
fi

CURRENT_VERSION=$(cat "$VERSION_FILE")

IFS='.' read -r major minor patch <<< "$CURRENT_VERSION"

NEW_VERSION="$major.$minor.$((patch + 1))"

echo "$NEW_VERSION" > "$VERSION_FILE"

echo "Building image..."

docker build -t "$IMAGE_NAME:latest" .

echo "Tagging image..."

docker tag "$IMAGE_NAME:latest" "$IMAGE_NAME:$NEW_VERSION"

echo "Pushing latest..."

docker push "$IMAGE_NAME:latest"

echo "Pushing version tag..."

docker push "$IMAGE_NAME:$NEW_VERSION"

echo "Successfully pushed:"
echo "$IMAGE_NAME:latest"
echo "$IMAGE_NAME:$NEW_VERSION"