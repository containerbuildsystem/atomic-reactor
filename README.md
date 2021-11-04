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

### For Fedora Users

```bash
sudo dnf install atomic-reactor python-atomic-reactor-koji
```

### From git

Clone this git repo and install Atomic Reactor using python installer:

```bash
git clone https://github.com/containerbuildsystem/atomic-reactor.git
cd atomic-reactor
sudo pip install .
```

You don't even need to install it. You may use it straight from git:

```bash
export PYTHONPATH="${REACTOR_PATH}:${PYTHONPATH}"
alias atomic-reactor="python ${REACTOR_PATH}/atomic-reactor/cli/main.py"
```

### Dependencies

- [docker-py][]
- The [koji plugin][] requires the `koji` package, which is not available on
  PyPI: you'll have to install it manually

```bash
sudo dnf install koji
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

## Usage

If you would like to build your images within build containers, you need to
obtain images for those containers. We call them build images. Atomic Reactor is
installed inside and used to take care of build itself.

You can either get the build image from Dockerhub or create it yourself.

### Getting build image from Dockerhub

Just use

```bash
docker pull slavek/atomic-reactor
```

This will pull the `buildroot` image with the latest Atomic Reactor commits.
Images with stable releases are available since version 1.3.3 and you can access
them by using the version specifier as a tag, such as

```bash
docker pull slavek/atomic-reactor:1.3.3
```

### Installation from git

```bash
atomic-reactor create-build-image --reactor-local-path ${PATH_TO_REACTOR_GIT} \
    ${PATH_TO_REACTOR_GIT}/images/dockerhost-builder buildroot
```

Why is it so long? Okay, let's get through. First thing is that Atomic Reactor
needs to install itself inside the build image. You can pick several sources for
Atomic Reactor: your local copy, (this) official upstream repo, your forked repo
or even distribution tarball. In the example above, we are using our locally
cloned git repo (`--reactor-local-path ${PATH_TO_REACTOR_GIT}`).

You have to provide Dockerfile too. Luckily these are part of upstream repo (see
folder [images][]). It's the first argument:
`${PATH_TO_REACTOR_GIT}/images/dockerhost-builder`.

And finally, you need to name the image: `buildroot`.

#### Getting Atomic Reactor from distribution

Or you can build the image using docker and install Atomic Reactor directly from
distribution:

```dockerfile
FROM fedora:latest
RUN dnf -y install docker-io git python-docker-py python-setuptools koji \
    atomic-reactor && dnf clean all
CMD ["atomic-reactor", "-v", "inside-build", "--input", "path"]
```

and command:

```bash
docker build -t buildroot-hostdocker .
```

#### Now you can build your images

As soon as our build image is built, we can start building stuff in it:

```bash
atomic-reactor build git --method hostdocker --build-image buildroot-hostdocker \
    --image test-image --uri "https://github.com/TomasTomecek/docker-hello-world.git"
```

The built image will be in the build container. Therefore, this example doesn't
make much sense. If you would like to access the built image, you should
probably push it to your registry and build it like this:

```bash
$ atomic-reactor build git --method hostdocker \
    --build-image buildroot-hostdocker \
    --image test-image \
    --target-registries 172.17.42.1:5000 \
    --uri "https://github.com/TomasTomecek/docker-hello-world.git"
```

Both of these examples use the `git` source provider (`atomic-reactor build
git`), which gets the source code to put in the image from a git repo. There are
also other providers:

- `path` ― uses source code from local path
- `json` ― accepts a path to build json file with all info needed for build

IP address `172.17.42.1` should be address of docker0 network interface. Update
it if yours is different. Also, don't forget to start the registry.

Bear in mind that you shouldn't mix build methods. If you use **hostdocker**
method with build image for **privileged** method, then it won't work.

## Further reading

- [Plugins](https://github.com/containerbuildsystem/atomic-reactor/blob/master/docs/plugins.md)
- [Plugin development](https://github.com/containerbuildsystem/atomic-reactor/blob/master/docs/plugin_development.md)
- [API](https://github.com/containerbuildsystem/atomic-reactor/blob/master/docs/api.md)
- [Build JSON](https://github.com/containerbuildsystem/atomic-reactor/blob/master/docs/build_json.md)

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
