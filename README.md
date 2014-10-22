dock
====

Simple python library for building docker images. It is written on top of [https://github.com/docker/docker-py](docker-py).

Images are built inside a docker image so you can preserve whole environment.

## Installation

```bash
pip install ./setup.py
```

## Usage

First, you have to built docker image for building other docker images, we call it build image.

```bash
$ pushd images/builder/
$ docker build --rm -t buildroot-fedora .
$ popd
```

And now you can easily build whatever image you want:

```python
db = DockerBuilder(
    git_url="https://github.com/TomasTomecek/docker-hello-world.git",
    local_tag="image-built-in-dock"
)
db.build("buildroot-fedora")
```

## TODO

* CLI client
* Implement plugin system
* Enable managing yum repos within build image
