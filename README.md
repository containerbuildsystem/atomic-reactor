# Atomic Reactor

[![build status]][build status link]
[![code health]][code health link]
[![coverage status]][coverage status link]
[![code quality: python]][code quality: python link]
[![total alerts]][total alerts link]

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

There are several build modes available:

- Building within a docker container using docker from host by mounting
  `docker.sock` inside the container
- Building within a privileged docker container (new instance of docker is
  running inside)
- Executing build within current environment

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

### Installation from RPM

```bash
atomic-reactor create-build-image --reactor-tarball-path \
    /usr/share/atomic-reactor/atomic-reactor.tar.gz \
    /usr/share/atomic-reactor/images/dockerhost-builder buildroot-fedora
```

Section above contains detailed description. Let's make this short.

1. `--reactor-tarball-path` — Atomic Reactor needs to install itself into build
   image: this is how you specify where Atomic Reactor gets its own sources
   (when installed via RPM, Atomic Reactor provides itself packaged as tarball
   at `/usr/share/atomic-reactor/atomic-reactor.tar.gz`)
1. First argument is path to **dockerfile** — dockerfiles for both methods are
   available at `/usr/share/atomic-reactor/images/`, just pick one
1. Finally, second argument names the build image

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
- [Building Atomic Reactor](https://github.com/containerbuildsystem/atomic-reactor/blob/master/docs/releasing.md)

[build status]: https://travis-ci.org/containerbuildsystem/atomic-reactor.svg?branch=master
[build status link]: https://travis-ci.org/containerbuildsystem/atomic-reactor
[code health]: https://landscape.io/github/containerbuildsystem/atomic-reactor/master/landscape.svg?style=flat
[code health link]: https://landscape.io/github/containerbuildsystem/atomic-reactor/master
[coverage status]: https://coveralls.io/repos/containerbuildsystem/atomic-reactor/badge.svg?branch=master
[coverage status link]: https://coveralls.io/r/containerbuildsystem/atomic-reactor?branch=master
[code quality: python]: https://img.shields.io/lgtm/grade/python/g/containerbuildsystem/atomic-reactor.svg?logo=lgtm&logoWidth=18
[code quality: python link]: https://lgtm.com/projects/g/containerbuildsystem/atomic-reactor/context:python
[total alerts]: https://img.shields.io/lgtm/alerts/g/containerbuildsystem/atomic-reactor.svg?logo=lgtm&logoWidth=18
[total alerts link]: https://lgtm.com/projects/g/containerbuildsystem/atomic-reactor/alerts
[koji]: https://github.com/containerbuildsystem/atomic-reactor/blob/master/docs/koji.md
[fedora packaging system]: http://fedoraproject.org/wiki/Package_maintenance_guide
[docker-py]: https://github.com/docker/docker-py
[koji plugin]: https://github.com/containerbuildsystem/atomic-reactor/blob/master/atomic_reactor/plugins/pre_koji.py
[images]: https://github.com/containerbuildsystem/atomic-reactor/tree/master/images
