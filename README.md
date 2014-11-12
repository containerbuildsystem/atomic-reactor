dock
====

Simple python library for building docker images. It is written on top of [docker-py](https://github.com/docker/docker-py).

It supports to building modes:

 * building within a docker container using docker from host by mounting docker.sock inside container
 * building within a privileged docker container

## Installation

```bash
pip install setup.py
```

`pip install ./setup.py` won't work.

## Usage

First, you have to built docker image for building other docker images, we call it build image.

```bash
$ make build-buildimage  # or quicker method q-build-buildimage -- it caches steps
```

And now can either run test suite...

```
py.test tests/test_dock.py::test_privileged_build
```

...or build whatever image you want:

```python
from dock.outer import PrivilegedDockerBuilder
db = PrivilegedDockerBuilder("buildroot-fedora", {
    "git_url": "github.com/TomasTomecek/docker-hello-world.git",
    "local_tag": "dock-test-image",
})
db.build()
```

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
SRPM=`tito build --srpm --test | egrep -o '/tmp/tito/dbs-server-.*\.src\.rpm'`
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

* CLI client
* Implement plugin system
* Enable managing yum repos within build image

