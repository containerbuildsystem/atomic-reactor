# Atomic Reactor

[![unittests status badge]][unittests status link]
[![coveralls status badge]][coveralls status link]
[![lgtm python badge]][lgtm python link]
[![lgtm alerts badge]][lgtm alerts link]
[![linters status badge]][linters status link]

Python library with command line interface for building docker images.

## Features

- Push image to registry when it's built
- Build inside a docker container (so your builds are separated between each
  other)
- git as a source to your Dockerfile (you may specify commit/branch and path to
  Dockerfile within the git repo)
- Collect build logs
- Integration with [koji][] build system
- Integration with [fedora packaging system][]
- Inject arbitrary yum repo inside Dockerfile (change source of your packages)
- Retag base image so it matches `FROM` instruction in Dockerfile
- Change base image (`FROM`) in your Dockerfile
- Run simple tests after your image is built

## Installation

### From git

Clone this git repo and install Atomic Reactor using python installer:

```bash
git clone https://github.com/containerbuildsystem/atomic-reactor.git
cd atomic-reactor
pip install . --user
```

You don't even need to install it. You may use it straight from git:

```bash
export PYTHONPATH="${REACTOR_PATH}:${PYTHONPATH}"
alias atomic-reactor="python ${REACTOR_PATH}/atomic-reactor/cli/main.py"
```

### Adding Dependencies

To add more Python dependencies, add them to the following files:

- [setup.py](setup.py)
- [requirements.in](requirements.in)
- [tests/requirements.in](tests/requirements.in) for test dependencies
- [requirements-devel.in](requirements-devel.in) for dependencies that are
  required during development

If you're wondering why you need to add dependencies to both files (setup.py
and one of the requirements files), see [install_requires vs requirements
files](https://packaging.python.org/discussions/install-requires-vs-requirements/).

To be able to build atomic-reactor with Cachito, we also need to keep the
build requirements
updated. Please follow [Cachito pip build dependencies](https://github.com/release-engineering/cachito/blob/master/docs/pip.md#build-dependencies)
for updating build requirements. Please note that the resulting requirements
will need to be pinned to older versions before moving to the next step to
avoid installation issues with newer
dependency versions.

Afterwards, pip-compile the dependencies via `make pip-compile` (you may need to
run `make venv` first, unless the venv already exists).

Additionally, if any of the newly added dependencies in the generated
`requirements*.txt` files need to be compiled from C code, please install any
missing C libraries in the Dockerfile(s) as well as the test.sh script

- [Dockerfile](Dockerfile)
- [test.sh](test.sh)

## Running tests

The prerequisite of running tests is to create a test environment container by
`test.sh`. For example:

```bash
OS=fedora OS_VERSION=35 PYTHON_VERSION=3 ACTION=test ./test.sh
```

When the container is ready and running, you have choice to test your changes
by executing `pytest` directly:

```bash
podman exec -it atomic-reactor-fedora-35-py3 python3 -m pytest tests/
```

or by `tox`:

```bash
podman exec -it atomic-reactor-fedora-35-py3 tox -e test
```

The `tox.ini` has defined several testenvs, use `tox -l` to check them.

## Usage

If you would like to build your images within build containers, you need to
obtain images for those containers. We call them build images. Atomic Reactor is
installed inside and used to take care of build itself.

You can either get the build image from Dockerhub or create it yourself.

## Further reading

- [Plugins](https://github.com/containerbuildsystem/atomic-reactor/blob/master/docs/plugins.md)
- [Plugin development](https://github.com/containerbuildsystem/atomic-reactor/blob/master/docs/plugin_development.md)
- [API](https://github.com/containerbuildsystem/atomic-reactor/blob/master/docs/api.md)

[coveralls status badge]: https://coveralls.io/repos/containerbuildsystem/atomic-reactor/badge.svg?branch=master
[coveralls status link]: https://coveralls.io/r/containerbuildsystem/atomic-reactor?branch=master
[lgtm python badge]: https://img.shields.io/lgtm/grade/python/g/containerbuildsystem/atomic-reactor.svg?logo=lgtm&logoWidth=18
[lgtm python link]: https://lgtm.com/projects/g/containerbuildsystem/atomic-reactor/context:python
[lgtm alerts badge]: https://img.shields.io/lgtm/alerts/g/containerbuildsystem/atomic-reactor.svg?logo=lgtm&logoWidth=18
[lgtm alerts link]: https://lgtm.com/projects/g/containerbuildsystem/atomic-reactor/alerts
[linters status badge]: https://github.com/containerbuildsystem/atomic-reactor/workflows/Linters/badge.svg?branch=master&event=push
[linters status link]: https://github.com/containerbuildsystem/atomic-reactor/actions?query=event%3Apush+branch%3Amaster+workflow%3A%22Linters%22
[unittests status badge]: https://github.com/containerbuildsystem/atomic-reactor/workflows/Unittests/badge.svg?branch=master&event=push
[unittests status link]: https://github.com/containerbuildsystem/atomic-reactor/actions?query=event%3Apush+branch%3Amaster+workflow%3A%22Unittests%22
[koji]: https://github.com/containerbuildsystem/atomic-reactor/blob/master/docs/koji.md
[fedora packaging system]: http://fedoraproject.org/wiki/Package_maintenance_guide
[docker-py]: https://github.com/docker/docker-py
[koji plugin]: https://github.com/containerbuildsystem/atomic-reactor/blob/master/atomic_reactor/plugins/pre_koji.py
[images]: https://github.com/containerbuildsystem/atomic-reactor/tree/master/images
