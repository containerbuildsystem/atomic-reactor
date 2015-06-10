dock
====

[![Build Status](https://travis-ci.org/DBuildService/dock.svg?branch=master)](https://travis-ci.org/DBuildService/dock)

Python library with command line interface for building docker images.

## Features

 * push image to registry when it's built
 * build inside a docker container (so your builds are separated between each other)
 * git as a source to your dockerfile (you may specify commit/branch and path to Dockerfile within the git repo)
 * collect build logs
 * integration with [koji](http://koji.fedoraproject.org/koji/) build system
 * integration with [fedora packaging system](http://fedoraproject.org/wiki/Package_maintenance_guide)
 * inject arbitrary yum repo inside dockerfile (change source of your packages)
 * retag base image so it matches `FROM` instruction in dockerfile
 * change base image (`FROM`) in your dockerfile
 * run simple tests after your image is built

There are several build modes available:

 * building within a docker container using docker from host by mounting `docker.sock` inside the container
 * building within a privileged docker container (new instance of docker is running inside)
 * executing build within current environment


## Installation

### for Fedora users

```bash
$ yum install dock dock-koji
```

### from git

Clone this git repo and install dock using python installer:

```bash
$ git clone https://github.com/DBuildService/dock.git
$ cd dock
$ sudo pip install .
```

You don't even need to install it. You may use it straight from git:

```bash
$ export PYTHONPATH="${DOCK_PATH}:${PYTHONPATH}"
$ alias dock="python ${DOCK_PATH}/dock/cli/main.py"
```

### Dependencies

 * [GitPython](https://github.com/gitpython-developers/GitPython/)
 * [docker-py](https://github.com/docker/docker-py).
 * [koji](https://github.com/DBuildService/dock/blob/master/dock/plugins/pre_koji.py) plugin requires `koji` package, which is not available on PyPI: you have to install it manually:
```
$ yum install koji
```

## Usage

If you would like to build your images within build containers, you need to obtain images for those containers. We call them build images. dock is installed inside and used to take care of build itself.

You can either get the build image from Dockerhub or create it yourself.

### getting build image from Dockerhub

Just use

```bash
$ docker pull slavek/buildroot
```

This will pull the `buildroot` image with the latest dock's commits. Images with stable releases are available since version 1.3.3 and you can access them by using the version specifier as a tag, such as

```bash
$ docker pull slavek/buildroot:1.3.3
```

### installation from git

```bash
$ dock create-build-image --dock-local-path ${PATH_TO_DOCK_GIT} ${PATH_TO_DOCK_GIT}/images/dockerhost-builder buildroot
```

Why is it so long? Okay, let's get through. First thing is that dock needs to install itself inside the build image. You can pick several sources for dock: your local copy, (this) official upstream repo, your forked repo or even distribution tarball. In the example above, we are using our locally cloned git repo (`--dock-local-path ${PATH_TO_DOCK_GIT}`).

You have to provide dockerfile too. Luckily these are part of upstream repo (see folder [images](https://github.com/DBuildService/dock/tree/master/images)). It's the first argument: `${PATH_TO_DOCK_GIT}/images/dockerhost-builder`.

And finally, you need to name the image: `buildroot`.

### installation from RPM

```bash
$ dock create-build-image --dock-tarball-path /usr/share/dock/dock.tar.gz /usr/share/dock/images/dockerhost-builder buildroot-fedora
```

Section above contains detailed description. Let's make this short.

1. `--dock-tarball-path` — dock needs to install itself into build image: this is how you specify where dock gets its own sources (when installed via RPM, dock provide itself packaged as tarball at `/usr/share/dock/dock.tar.gz`)
2. first argument is path do _dockerfile_ — dockerfiles for both methods are available at `/usr/share/dock/images/`, just pick one
3. and finally, second argument names the build image

#### getting dock from distribution

Or you can build the image using docker and install dock directly from distribution:

```dockerfile
FROM fedora:latest
RUN yum -y install docker-io git python-docker-py python-setuptools GitPython koji dock
CMD ["dock", "-v", "inside-build", "--input", "path"]
```

and command:

```
$ docker build -t buildroot-hostdocker .
```

#### And now you can build your images!

As soon as our build image is built, we can start building stuff in it:

```bash
$ dock build --method hostdocker --build-image buildroot --image test-image --git-url "https://github.com/TomasTomecek/docker-hello-world.git"
```

Built image will be in the build container. Therefore this example doesn't make much sense. If you would like to access the built image, you should probably push it to your registry and build it like this:

```bash
$ dock build --method hostdocker \
             --build-image buildroot \
             --image test-image \
             --target-registries 172.17.42.1:5000 \
             --git-url "https://github.com/TomasTomecek/docker-hello-world.git"
```

IP address `172.17.42.1` should be address of docker0 network interface. Update it if yours is different. Also, don't forget to start the registry.


Bear in mind that you shouldn't mix build methods: if you use _hostdocker_ method with build image for _privileged_ method, it won't work.


## Further reading

 * [plugins](https://github.com/DBuildService/dock/blob/master/docs/plugins.md)
 * [plugin development](https://github.com/DBuildService/dock/blob/master/docs/plugin_development.md)
 * [api](https://github.com/DBuildService/dock/blob/master/docs/api.md)
 * [build json](https://github.com/DBuildService/dock/blob/master/docs/build_json.md)
 * [building dock](https://github.com/DBuildService/dock/blob/master/docs/releasing.md)
