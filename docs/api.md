# API

Atomic-Reactor has a proper Python API. You can use it in your scripts or
services without invoking a shell:

```python
from atomic_reactor.api import build_image_in_privileged_container

response = build_image_in_privileged_container(
    "privileged-buildroot",
    source={
        'provider': 'git',
        'uri': 'https://github.com/TomasTomecek/docker-hello-world.git',
    },
    image="atomic-reactor-test-image",
)
```

## Source

The `source` argument to API functions specifies how to obtain the source code
that should be put in the image.

- `provider`
  - `git`
  - `path`
- `uri`
  - If `provider` is `git`, `uri` is a Git repo URI
  - If `provider` is `path`, `uri` is path in format `file:///abs/path`
- `dockerfile_path` (optional): Path to Dockerfile inside a directory obtained
  from URI; `./` is default
- `provider_params` (optional):
  - If `provider` is `git`, `provider_params` can contain key `git_commit` (git
    commit to put inside the image)
  - There are no params for `path` as of now

For example:

```python
git_source = {
    'provider': 'git',
    'uri': 'https://github.com/foo/bar.git',
    'dockerfile_path': 'spam/spam/',
    'provider_params': {'git_commit': 'abcdefg'}
}

path_params = {
    'provider': 'path',
    'uri': 'file:///path/to/directory',
    'dockerfile_path': 'foo/',
}
```

## Module 'api'

Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Python API for atomic-reactor. This is the official way of interacting with
atomic-reactor.

### 'api' Functions

```python
build_image_here(source, image,
                 parent_registry=None,
                 target_registries=None,
                 parent_registry_insecure=False,
                 target_registries_insecure=False,
                 dont_pull_base_image=False,
                 **kwargs):
```

```text
    build image from provided dockerfile (specified by `source`) in current environment

    :param source: dict, where/how to get source code to put in image
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param parent_registry: str, registry to pull base image from
    :param target_registries: list of str, list of registries to push image to (might change in future)
    :param parent_registry_insecure: bool, allow connecting to parent registry over plain http
    :param target_registries_insecure: bool, allow connecting to target registries over plain http
    :param dont_pull_base_image: bool, don't pull or update base image specified in dockerfile

    :return: BuildResults
```

```python
build_image_in_privileged_container(build_image, source, image,
                                    parent_registry=None,
                                    target_registries=None,
                                    push_buildroot_to=None,
                                    parent_registry_insecure=False,
                                    target_registries_insecure=False,
                                    dont_pull_base_image=False,
                                    **kwargs)
```

```text
    build image from provided dockerfile (specified by `source`) in privileged container by
    running another docker instance inside the container

    :param build_image: str, image where target image should be built
    :param source: dict, where/how to get source code to put in image
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param parent_registry: str, registry to pull base image from
    :param target_registries: list of str, list of registries to push image to (might change in future)
    :param push_buildroot_to: str, repository where buildroot should be pushed
    :param parent_registry_insecure: bool, allow connecting to parent registry over plain http
    :param target_registries_insecure: bool, allow connecting to target registries over plain http
    :param dont_pull_base_image: bool, don't pull or update base image specified in dockerfile

    :return: BuildResults
```

```python
build_image_using_hosts_docker(build_image, source, image,
                               parent_registry=None,
                               target_registries=None,
                               push_buildroot_to=None,
                               parent_registry_insecure=False,
                               target_registries_insecure=False,
                               dont_pull_base_image=False,
                               **kwargs)
```

```text
    build image from provided dockerfile (specified by `source`) in privileged container
    using docker from host

    :param build_image: str, image where target image should be built
    :param source: dict, where/how to get source code to put in image
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param parent_registry: str, registry to pull base image from
    :param target_registries: list of str, list of registries to push image to (might change in future)
    :param push_buildroot_to: str, repository where buildroot should be pushed
    :param parent_registry_insecure: bool, allow connecting to parent registry over plain http
    :param target_registries_insecure: bool, allow connecting to target registries over plain http
    :param dont_pull_base_image: bool, don't pull or update base image specified in dockerfile

    :return: BuildResults
```

## Module 'core'

### 'core' Classes

#### DockerTasker class

##### DockerTasker Instance variables

- d
- last_logs
  - `logs from last operation`

##### DockerTasker Methods

```python
__init__(self,
         base_url=None,
         timeout=120,
         **kwargs)
```

```text
    Constructor

    :param base_url: str, docker connection URL
    :param timeout: int, timeout for docker client
```

```python
build_image_from_git(self, url, image,
                     git_path=None,
                     git_commit=None,
                     copy_dockerfile_to=None,
                     stream=False,
                     use_cache=False)
```

```text
    build image from provided url and tag it

    this operation is asynchronous and you should consume returned generator in order to wait
    for build to finish

    :param url: str
    :param image: ImageName, name of the resulting image
    :param git_path: str, path to dockerfile within gitrepo
    :param copy_dockerfile_to: str, copy dockerfile to provided path
    :param stream: bool, True returns generator, False returns str
    :param use_cache: bool, True if you want to use cache
    :return: generator
```

```python
build_image_from_path(self, path, image,
                      stream=False,
                      use_cache=False,
                      remove_im=True)
```

```text
    build image from provided path and tag it

    this operation is asynchronous and you should consume returned generator in
        order to wait
    for build to finish

    :param path: str
    :param image: ImageName, name of the resulting image
    :param stream: bool, True returns generator, False returns str
    :param use_cache: bool, True if you want to use cache
    :param remove_im: bool, remove intermediate containers produced during docker build
    :return: generator
```

```python
commit_container(self, container_id,
                 image=None
                 message=None)
```

```text
    create image from provided container

    :param container_id: str
    :param image: ImageName
    :param message: str
    :return: image_id
```

```python
get_image_info_by_image_id(self, image_id)
```

```text
    using `docker images`, provide information about an image

    :param image_id: str, hash of image to get info
    :return: str or None
```

```python
get_image_info_by_image_name(self, image,
                             exact_tag=True)
```

```text
    using `docker images`, provide information about an image

    :param image: ImageName, name of image
    :param exact_tag: bool, if false then return info for all images of the
                        given name regardless what their tag is
    :return: list of dicts
```

```python
get_info(self)
```

```text
    get info about used docker environment

    :return: dict, json output of `docker info`
```

```python
get_version(self)
```

```text
    get version of used docker environment

    :return: dict, json output of `docker version`
```

```python
image_exists(self, image_id)
```

```text
    does provided image exists?

    :param image_id: str or ImageName
    :return: True if exists, False if not
```

```python
inspect_image(self, image_id)
```

```text
    return detailed metadata about provided image (see 'man docker-inspect')

    :param image_id: str or ImageName, id or name of the image
    :return: dict
```

```python
login(self, registry, docker_secret_path)
```

```text
    login to docker registry

    :param registry: registry name
    :param docker_secret_path: path to docker config directory
```

```python
logs(self, container_id,
     stderr=True,
     stream=True)
```

```text
    acquire output (stdout, stderr) from provided container

    :param container_id: str
    :param stderr: True, False
    :param stream: if True, return as generator
    :return: either generator, or list of strings
```

```python
pull_image(self, image,
           insecure=False)
```

```text
    pull provided image from registry

    :param image_name: ImageName, image to pull
    :param insecure: bool, allow connecting to registry over plain http
    :return: str, image (reg.om/img:v1)
```

```python
push_image(self, image,
           insecure=False)
```

```text
    push provided image to registry

    :param image: ImageName
    :param insecure: bool, allow connecting to registry over plain http
    :return: str, logs from push
```

```python
remove_container(self, container_id,
                 force=False)
```

```text
    Remove provided container from filesystem

    :param container_id: str
    :param force: bool, remove forcefully?
    :return: None
```

```python
remove_image(self, image_id,
             force=False,
             noprune=False)
```

```text
    Remove provided image from filesystem

    :param image_id: str or ImageName
    :param noprune: bool, keep untagged parents?
    :param force: bool, force remove -- just trash it no matter what
    :return: None
```

```python
run(self, image,
    command=None,
    create_kwargs=None,
    start_kwargs=None)
```

```text
    Create container from provided image and start it

    For more info, see documentation of REST API calls:
        - containers/{}/start
        - container/create

    :param image: ImageName or string, name or id of the image
    :param command: str
    :param create_kwargs: dict, kwargs for docker.create_container
    :param start_kwargs: dict, kwargs for docker.start
    :return: str, container id
```

```python
tag_and_push_image(self, image, target_image,
                   insecure=False,
                   force=False,
                   dockercfg=None)
```

```text
    tag provided image and push it to registry

    :param image: str or ImageName, image id or name
    :param target_image: ImageName, img
    :param insecure: bool, allow connecting to registry over plain http
    :param force: bool, force the tag?
    :param dockercfg: path to docker config
    :return: str, image (reg.com/img:v1)
```

```python
tag_image(self, image, target_image,
          force=False)
```

```text
    tag provided image with specified image_name, registry and tag

    :param image: str or ImageName, image to tag
    :param target_image: ImageName, new name for the image
    :param force: bool, force tag the image?
    :return: str, image (reg.om/img:v1)
```

```python
wait(self, container_id)
```

```text
    wait for container to finish the job (may run infinitely)

    :param container_id: str
    :return: int, exit code
```

## Module 'inner'

### 'inner' Classes

#### DockerBuildWorkflow class

This class defines a workflow for building images

1. Pull image from registry
1. Tag it properly if needed
1. Obtain source
1. Build image
1. Tag it
1. Push it to registries

##### DockerBuildWorkflow instance variables

- autorebuild_canceled
- base_image_inspect
- build_result
- build_process_failed
  - `Has any aspect of the build process failed?`
- builder
- built_image_inspect
- exit_plugins_conf
- exit_results
- exported_image_sequence
- files
- image
- kwargs
- openshift_build_selflink
- plugin_failed
- plugin_files
- plugin_workspace
- plugins_durations
- plugins_errors
- plugins_timestamps
- postbuild_plugins_conf
- postbuild_results
- prebuild_plugins_conf
- prebuild_results
- prepub_results
- prepublish_plugins_conf
- pulled_base_images
- push_conf
- source
- tag_conf

##### DockerBuildWorkflow methods

```python
__init__(self, source, image,
         prebuild_plugins=None,
         prepublish_plugins=None,
         postbuild_plugins=None,
         exit_plugins=None,
         plugin_files=None,
         openshift_build_selflink=None,
         kwargs)
```

```text
    Constructor

    :param source: dict, where/how to get source code to put in image
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param prebuild_plugins: dict, arguments for pre-build plugins
    :param prepublish_plugins: dict, arguments for test-build plugins
    :param postbuild_plugins: dict, arguments for post-build plugins
    :param plugin_files: list of str, load plugins also from these files
    :param openshift_build_selflink: str, link to openshift build (if we're actually
            running on openshift) without the actual hostname/IP address
```

```python
build_docker_image(self)
```

```text
    build docker image

    :return: BuildResults
```

## Module 'build'

### 'build' Classes

#### InsideBuilder class

This is expected to run within container

##### InsideBuilder instance variables

- base_image_id
- built_image_info
- image
- image_id
- last_logs
  - `logs from last operation`
- source
- tasker

##### InsideBuilder methods

```python
__init__(self, source, image, kwargs)
```

```text
    Constructor
```

```python
build(self)
```

```text
    build image inside current environment;
    it's expected this may run within (privileged) docker container

    :return: image string (e.g. fedora-python:34)
```

```python
get_base_image_info(self)
```

```text
    query docker about base image

    :return dict
```

```python
get_built_image_info(self)
```

```text
    query docker about built image

    :return dict
```

```python
inspect_base_image(self)
```

```text
    inspect base image

    :return: dict
```

```python
inspect_built_image(self)
```

```text
    inspect built image

    :return: dict
```

```python
set_base_image(self, base_image)
```
