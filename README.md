Atomic Reactor
==============

[![Build Status](https://travis-ci.org/projectatomic/atomic-reactor.svg?branch=master)](https://travis-ci.org/projectatomic/atomic-reactor)
[![Code Health](https://landscape.io/github/projectatomic/atomic-reactor/master/landscape.svg?style=flat)](https://landscape.io/github/projectatomic/atomic-reactor/master)
[![Coverage Status](https://coveralls.io/repos/projectatomic/atomic-reactor/badge.svg?branch=master)](https://coveralls.io/r/projectatomic/atomic-reactor?branch=master)

Python library with command line interface for building docker images.

## Features

 * push image to registry when it's built
 * build inside a docker container (so your builds are separated between each other)
 * git as a source to your Dockerfile (you may specify commit/branch and path to Dockerfile within the git repo)
 * collect build logs
 * integration with [koji](http://koji.fedoraproject.org/koji/) build system
 * integration with [fedora packaging system](http://fedoraproject.org/wiki/Package_maintenance_guide)
 * inject arbitrary yum repo inside Dockerfile (change source of your packages)
 * retag base image so it matches `FROM` instruction in Dockerfile
 * change base image (`FROM`) in your Dockerfile
 * run simple tests after your image is built

There are several build modes available:

 * building within a docker container using docker from host by mounting `docker.sock` inside the container
 * building within a privileged docker container (new instance of docker is running inside)
 * executing build within current environment


## Installation

### for Fedora users

```bash
$ yum install atomic-reactor python-atomic-reactor-koji
```

### from git

Clone this git repo and install Atomic Reactor using python installer:

```bash
$ git clone https://github.com/projectatomic/atomic-reactor.git
$ cd atomic-reactor
$ sudo pip install .
```

You don't even need to install it. You may use it straight from git:

```bash
$ export PYTHONPATH="${REACTOR_PATH}:${PYTHONPATH}"
$ alias atomic-reactor="python ${REACTOR_PATH}/atomic-reactor/cli/main.py"
```

### Dependencies

 * [GitPython](https://github.com/gitpython-developers/GitPython/)
 * [docker-py](https://github.com/docker/docker-py).
 * [koji](https://github.com/projectatomic/atomic-reactor/blob/master/atomic_reactor/plugins/pre_koji.py) plugin requires `koji` package, which is not available on PyPI: you have to install it manually:
```
$ yum install koji
```

## Usage

If you would like to build your images within build containers, you need to obtain images for those containers. We call them build images. Atomic Reactor is installed inside and used to take care of build itself.

You can either get the build image from Dockerhub or create it yourself.

### getting build image from Dockerhub

Just use

```bash
$ docker pull slavek/atomic-reactor
```

This will pull the `buildroot` image with the latest Atomic Reactor commits. Images with stable releases are available since version 1.3.3 and you can access them by using the version specifier as a tag, such as

```bash
$ docker pull slavek/atomic-reactor:1.3.3
```

### installation from git

```bash
$ atomic-reactor create-build-image --reactor-local-path ${PATH_TO_REACTOR_GIT} ${PATH_TO_REACTOR_GIT}/images/dockerhost-builder buildroot
```

Why is it so long? Okay, let's get through. First thing is that Atomic Reactor needs to install itself inside the build image. You can pick several sources for Atomic Reactor: your local copy, (this) official upstream repo, your forked repo or even distribution tarball. In the example above, we are using our locally cloned git repo (`--reactor-local-path ${PATH_TO_REACTOR_GIT}`).

You have to provide Dockerfile too. Luckily these are part of upstream repo (see folder [images](https://github.com/projectatomic/atomic-reactor/tree/master/images)). It's the first argument: `${PATH_TO_REACTOR_GIT}/images/dockerhost-builder`.

And finally, you need to name the image: `buildroot`.

### installation from RPM

```bash
$ atomic-reactor create-build-image --reactor-tarball-path /usr/share/atomic-reactor/atomic-reactor.tar.gz /usr/share/atomic-reactor/images/dockerhost-builder buildroot-fedora
```

Section above contains detailed description. Let's make this short.

1. `--reactor-tarball-path` — Atomic Reactor needs to install itself into build image: this is how you specify where Atomic Reactor gets its own sources (when installed via RPM, Atomic Reactor provides itself packaged as tarball at `/usr/share/atomic-reactor/atomic-reactor.tar.gz`)
2. first argument is path do _dockerfile_ — dockerfiles for both methods are available at `/usr/share/atomic-reactor/images/`, just pick one
3. and finally, second argument names the build image

#### getting Atomic Reactor from distribution

Or you can build the image using docker and install Atomic Reactor directly from distribution:

```dockerfile
FROM fedora:latest
RUN yum -y install docker-io git python-docker-py python-setuptools GitPython koji atomic-reactor
CMD ["atomic-reactor", "-v", "inside-build", "--input", "path"]
```

and command:

```
$ docker build -t buildroot-hostdocker .
```

#### And now you can build your images!

As soon as our build image is built, we can start building stuff in it:

```bash
$ atomic-reactor build git --method hostdocker --build-image buildroot --image test-image --uri "https://github.com/TomasTomecek/docker-hello-world.git"
```

Built image will be in the build container. Therefore this example doesn't make much sense. If you would like to access the built image, you should probably push it to your registry and build it like this:

```bash
$ atomic-reactor build git --method hostdocker \
             --build-image buildroot \
             --image test-image \
             --target-registries 172.17.42.1:5000 \
             --uri "https://github.com/TomasTomecek/docker-hello-world.git"
```

Both of these examples use the `git` source provider (`atomic-reactor build git`), which gets the source code to put in the image from a git repo. There are also other providers:
 * `path` - uses source code from local path
 * `json` - accepts a path to build json file with all info needed for build

IP address `172.17.42.1` should be address of docker0 network interface. Update it if yours is different. Also, don't forget to start the registry.


Bear in mind that you shouldn't mix build methods: if you use _hostdocker_ method with build image for _privileged_ method, it won't work.


## Further reading

 * [plugins](https://github.com/projectatomic/atomic-reactor/blob/master/docs/plugins.md)
 * [plugin development](https://github.com/projectatomic/atomic-reactor/blob/master/docs/plugin_development.md)
 * [api](https://github.com/projectatomic/atomic-reactor/blob/master/docs/api.md)
 * [build json](https://github.com/projectatomic/atomic-reactor/blob/master/docs/build_json.md)
 * [building Atomic Reactor](https://github.com/projectatomic/atomic-reactor/blob/master/docs/releasing.md)

## Contact

Get in touch with us via [atomic-devel@projectatomic.io](https://lists.projectatomic.io/mailman/listinfo/atomic-devel) mailing list.
