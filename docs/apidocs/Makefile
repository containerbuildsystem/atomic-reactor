.PHONY: build api.md

default: build

ROOT_DIR := $(shell dirname $(realpath $(lastword $(MAKEFILE_LIST))))

clean:
	rm -v $(ROOT_DIR)/../api.md 2>/dev/null || :

build: clean api.md
	mv $(ROOT_DIR)/api.md $(ROOT_DIR)/../

api.md:
	@echo "Generating documentation for API"
	cat $(ROOT_DIR)/top.md >api.md
	pdoc $(ROOT_DIR)/../../atomic_reactor/api.py --template-dir $(ROOT_DIR)/ >>api.md
	pdoc $(ROOT_DIR)/../../atomic_reactor/core.py DockerTasker --template-dir $(ROOT_DIR) >>api.md
	pdoc $(ROOT_DIR)/../../atomic_reactor/inner.py DockerBuildWorkflow --template-dir $(ROOT_DIR) >>api.md
	pdoc $(ROOT_DIR)/../../atomic_reactor/build.py InsideBuilder --template-dir $(ROOT_DIR) >>api.md
