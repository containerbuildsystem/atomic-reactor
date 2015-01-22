dock
====

Simple python library with command line interface for building docker images. It is written on top of [docker-py](https://github.com/docker/docker-py).

It supports several building modes:

 * building within a docker container using docker from host by mounting docker.sock inside container
 * building within a privileged docker container (new instance of docker is running inside)
 * building images in current environemt

## Installation

### via COPR

```bash
$ dnf copr enable jdornak/DBuildService
$ dnf install dock
```

### from git

Clone this git repo and install it using python installer:

```bash
$ git clone https://github.com/DBuildService/dock.git
$ cd dock
$ sudo pip install .
```

dock requires `GitPython` and `docker-py` (koji plugin requires `koji` package, you have to install it manually: `yum install koji`); pip should install those.

## Usage

If you would like to build your images within containers, you need to obtain images for those containers. We call them build images. dock is installed inside and used to take care of build itself.

At some point, these will be available on docker hub, but right now, you need to build them yourself.

### installation from git

```bash
$ dock create-build-image --dock-local-path ${PATH_TO_DOCK_GIT} ${PATH_TO_DOCK_GIT}/images/privileged-builder privileged-buildroot
```

Why is it so long? Okay, let's get through. First thing is that dock needs to install itself inside the build image. You can pick several sources for dock: you local copy, (this) official upstream repo, your forked repo or even distribution tarball. In the example above, we are using our locally cloned git repo (`--dock-local-path ${PATH_TO_DOCK_GIT}`).

You have to provide dockerfile too. Luckily these are part of upstream repo (see folder images). It's the first argument: `${PATH_TO_DOCK_GIT}/images/privileged-build`.

And finally, you need to name the image: `privileged-buildroot`.

### installation from RPM

```bash
$ dock create-build-image --dock-tarball-path /usr/share/dock/dock.tar.gz /usr/share/dock/images/privileged-builder buildroot-fedora
```

Section above contains detailed description. Let's make this short.

1. `--dock-tarball-path` — dock needs to install itself into build image: this is how you specify where dock gets its own sources (when installed via RPM, dock provide itself packaged as tarball at `/usr/share/dock/dock.tar.gz`)
2. first argument is path do _dockerfile_ — dockerfiles for both methods are available at `/usr/share/dock/images/`, just pick one
3. and finally, second argument names the build image

#### And now you can build your images!

As soon as our build image is built, we can start building stuff in it:

```bash
$ dock build --method privileged --build-image privileged-buildroot --image test-image --git-url "https://github.com/TomasTomecek/docker-hello-world.git"
```

Built image will be in the build container. Therefore this example doesn't make much sense. If you would like to access the built image, you should probably push it to your registry and build it like this:

```bash
$ dock build --method privileged \
             --build-image privileged-buildroot \
             --image test-image \
             --target-registries 172.17.42.1:5000 \
             --git-url "https://github.com/TomasTomecek/docker-hello-world.git"
```

IP address `172.17.42.1` should be address of docker0 interface. Update it if yours is different. Also, don't forget to start registry.


Bear in mind that you shouldn't mix build methods: if you use _hostdocker_ method with build image for _privileged_ method, it won't work.

## API

dock has proper python API. You can use it in your scripts or service without invoking shell:

```python
from dock.api import build_image_in_privileged_container
response = build_image_in_privileged_container(
    "privileged-buildroot",
    git_url="https://github.com/TomasTomecek/docker-hello-world.git",
    image="dock-test-image",
)
# response contains a lot of useful information: logs, information about images, plugin results
```

## build.json

If you want to take advantage of _inner_ part logic of dock, you can do that pretty easily. All you need to know, is the structure of json, which is used within build container. Here it is:

```json
{
    "git_url": "http://...",
    "image": "my-test-image",
    "git_dockerfile_path": "django/",
    "git_commit": "devel",
    "parent_registry": "registry.example.com:5000",
    "target_registries": ["registry.example2.com:5000"],
    "prebuild_plugins": {"dockerfile_content": null},
    "postbuild_plugins": {"all_rpm_packages": "my-test-image"}
}
```

 * git_url - string, path to git repo with Dockerfile
 * image - string, tag for built image
 * git_dockerfile_path - string, optional, path to dockerfile within git repo
 * git_commit - string, optional, git commit to checkout
 * parent_registry - string, optional, registry to pull base image from
 * target_registries - list of strings, optional, registries where built image should be pushed
 * prebuild_plugins - dict, arguments for pre-build plugins
 * postbuild_plugins - dict, arguments for post-build plugins

It is read from two places at the moment:

1. environemtn variable `BUILD_JSON`
2. `/run/share/build.json`

## RPM build

Install tito and mock:

```bash
dnf install tito mock
```

Build RPM locally:

```bash
# build from the latest tagged release
tito build --rpm
# or build from the latest commit
tito build --rpm --test
```

Build RPM using mock:

```bash
SRPM=`tito build --srpm --test | egrep -o '/tmp/tito/dock-.*\.src\.rpm'`
sudo mock -r fedora-21-x86_64 $SRPM
```

## Submit Build in Copr

First you need to set up rel-eng/releasers.conf:

```bash
sed "s/<USERNAME>/$USERNAME/" < rel-eng/releasers.conf.template > rel-eng/releasers.conf
```

Now you may submit build:

```bash
# submit build from latest commit
tito release copr-test
# or submit build from the latest tag
tito release copr
```

## TODO

* Enable managing repositories within built image (changing source of packages during build without dockerfile modification)
* Add support for different registries

