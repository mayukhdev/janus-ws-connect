.PHONY: all build run run-only clean

# Get the bit version from the system
BIT_VERSION := $(shell uname -m)
VERSION := $(BIT_VERSION)
TAG := latest
IMAGE_NAME := janus-python-$(VERSION)
IMAGE_TAG := $(VERSION):$(TAG)

# Default target
all: run

# Build the Docker image
build:
	@echo "Building Docker image with tag $(IMAGE_TAG)"
	@docker rmi $(IMAGE_NAME):$(TAG) || true
	@docker build -t $(IMAGE_NAME):$(TAG) .

# Run the Docker Compose (with building)
run: build run-only

# Run the Docker Compose without building
run-only:
	@echo "Running Docker Compose"
	@docker run -d -it --rm  --name janus_python_container --net=host -v .:/usr/src/app $(IMAGE_NAME):$(TAG)

# Clean up Docker images
clean:
	@docker stop janus_python_container
	@docker rmi $(IMAGE_NAME):$(TAG) || true

restart: clean run