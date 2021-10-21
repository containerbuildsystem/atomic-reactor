PYTHON_VERSION_VENV ?= python3.8

.PHONY: venv
venv:
	virtualenv --python=${PYTHON_VERSION_VENV} venv
	venv/bin/pip install --upgrade pip

pip-compile: venv/bin/pip-compile
	venv/bin/pip-compile --output-file=requirements.txt requirements.in
	venv/bin/pip-compile --generate-hashes --output-file=tests/requirements.txt tests/requirements.in
	# --allow-unsafe: because we are specifying pip as a dependency
	venv/bin/pip-compile --generate-hashes --allow-unsafe --output-file=requirements-pip.txt \
	requirements-pip.in
	# --allow-unsafe: because we are specifying pip as a dependency
	venv/bin/pip-compile --generate-hashes --allow-unsafe --output-file=requirements-build.txt \
	requirements-build.in
	venv/bin/pip-compile --output-file=requirements-devel.txt requirements-devel.in

venv/bin/pip-compile: venv
	venv/bin/pip install pip-tools
