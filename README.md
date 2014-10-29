dock
====

Simple python library for building docker images. It is written on top of [https://github.com/docker/docker-py](docker-py).

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
$ make build-buildimage  # or quicker method q-build-image -- it caches steps

$ py.test tests/test_dock.py::test_privileged_build
```

And now can either run test suite...

```
py.test tests/test_dock.py::test_privileged_build
```

...or build whatever image you want:

```python
db = PrivilegedDockerBuilder("buildroot-fedora", {
    "git_url": "github.com/TomasTomecek/docker-hello-world.git",
    "local_tag": "dock-test-image",
})
db.build()
```

## TODO

* CLI client
* Implement plugin system
* Enable managing yum repos within build image
