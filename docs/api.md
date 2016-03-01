# API

atomic-reactor has proper python API. You can use it in your scripts or services without invoking shell:

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

The `source` argument to API functions specifies how to obtain the source code that should
be put in the image. It has keys `provider`, `uri`, `dockerfile_path` and `provider_params`.

* `provider` can be `git` or `path`
* `uri`
  * if `provider` is `git`, `uri` is a Git repo URI
  * if `provider` is `path`, `uri` is path in format `file:///abs/path`
* `dockerfile_path` (optional) is path to Dockerfile inside a directory obtained from URI;
  `./` is default
* `provider_params` (optional)
  * if `provider` is `git`, `provider_params` can contain key `git_commit` (git commit
    to put inside the image)
  * there are no params for `path` as of now

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

Python API for atomic-reactor. This is the official way of interacting with atomic-reactor.

### Functions
**build\_image\_here**(source, image, parent\_registry=None, target\_registries=None, parent\_registry\_insecure=False, target\_registries\_insecure=False, \*\*kwargs):
```
    build image from provided dockerfile (specified by `source`) in current environment

    :param source: dict, where/how to get source code to put in image
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param parent_registry: str, registry to pull base image from
    :param target_registries: list of str, list of registries to push image to (might change in future)
    :param parent_registry_insecure: bool, allow connecting to parent registry over plain http
    :param target_registries_insecure: bool, allow connecting to target registries over plain http

    :return: BuildResults
```

**build\_image\_in\_privileged\_container**(build\_image, source, image, parent\_registry=None, target\_registries=None, push\_buildroot\_to=None, parent\_registry\_insecure=False, target\_registries\_insecure=False, \*\*kwargs):
```
    build image from provided dockerfile (specified by `source`) in privileged image

    :param build_image: str, image where target image should be built
    :param source: dict, where/how to get source code to put in image
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param parent_registry: str, registry to pull base image from
    :param target_registries: list of str, list of registries to push image to (might change in future)
    :param push_buildroot_to: str, repository where buildroot should be pushed
    :param parent_registry_insecure: bool, allow connecting to parent registry over plain http
    :param target_registries_insecure: bool, allow connecting to target registries over plain http

    :return: BuildResults
```

**build\_image\_using\_hosts\_docker**(build\_image, source, image, parent\_registry=None, target\_registries=None, push\_buildroot\_to=None, parent\_registry\_insecure=False, target\_registries\_insecure=False, \*\*kwargs):
```
    build image from provided dockerfile (specified by `source`) in container
    using docker from host

    :param build_image: str, image where target image should be built
    :param source: dict, where/how to get source code to put in image
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param parent_registry: str, registry to pull base image from
    :param target_registries: list of str, list of registries to push image to (might change in future)
    :param push_buildroot_to: str, repository where buildroot should be pushed
    :param parent_registry_insecure: bool, allow connecting to parent registry over plain http
    :param target_registries_insecure: bool, allow connecting to target registries over plain http

    :return: BuildResults
```
## Module 'core'

### Classes
### `class` DockerTasker 
#### Instance variables
* last_logs  
`logs from last operation `

#### Methods
**\_\_init\_\_**(self, base\_url=None, \*\*kwargs):

**build\_image\_from\_git**(self, url, image, git\_path=None, git\_commit=None, copy\_dockerfile\_to=None, stream=False, use\_cache=False):
```
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

**build\_image\_from\_path**(self, path, image, stream=False, use\_cache=False, remove\_im=True):
```
    build image from provided path and tag it

    this operation is asynchronous and you should consume returned generator in order to wait
    for build to finish

    :param path: str
    :param image: ImageName, name of the resulting image
    :param stream: bool, True returns generator, False returns str
    :param use_cache: bool, True if you want to use cache
    :param remove_im: bool, remove intermediate containers produced during docker build
    :return: generator
```

**commit\_container**(self, container\_id, image=None, message=None):
```
    create image from provided container

    :param container_id: str
    :param image: ImageName
    :param message: str
    :return: image_id
```

**get\_image\_info\_by\_image\_id**(self, image\_id):
```
    using `docker images`, provide information about an image

    :param image_id: str, hash of image to get info
    :return: str or None
```

**get\_image\_info\_by\_image\_name**(self, image, exact\_tag=True):
```
    using `docker images`, provide information about an image

    :param image: ImageName, name of image
    :param exact_tag: bool, if false then return info for all images of the 
                      given name regardless what their tag is
    :return: list of dicts
```

**image\_exists**(self, image\_id):
```
    does provided image exists?

    :param image_id: str or ImageName
    :return: True if exists, False if not
```

**inspect\_image**(self, image\_id):
```
    return detailed metadata about provided image (see 'man docker-inspect')

    :param image_id: str or ImageName, id or name of the image
    :return: dict
```

**logs**(self, container\_id, stderr=True, stream=True):
```
    acquire output (stdout, stderr) from provided container

    :param container_id: str
    :param stderr: True, False
    :param stream: if True, return as generator
    :return: either generator, or list of strings
```

**pull\_image**(self, image, insecure=False):
```
    pull provided image from registry

    :param image_name: ImageName, image to pull
    :param insecure: bool, allow connecting to registry over plain http
    :return: str, image (reg.om/img:v1)
```

**push\_image**(self, image, insecure=False):
```
    push provided image to registry

    :param image: ImageName
    :param insecure: bool, allow connecting to registry over plain http
    :return: str, logs from push
```

**remove\_container**(self, container\_id, force=False):
```
    remove provided container from filesystem

    :param container_id: str
    :param force: bool, remove forcefully?
    :return: None
```

**remove\_image**(self, image\_id, force=False, noprune=False):
```
    remove provided image from filesystem

    :param image_id: str or ImageName
    :param noprune: bool, keep untagged parents?
    :param force: bool, force remove -- just trash it no matter what
    :return: None
```

**run**(self, image, command=None, create\_kwargs=None, start\_kwargs=None):
```
    create container from provided image and start it

    for more info, see documentation of REST API calls:
     * containers/{}/start
     * container/create

    :param image: ImageName or string, name or id of the image
    :param command: str
    :param create_kwargs: dict, kwargs for docker.create_container
    :param start_kwargs: dict, kwargs for docker.start
    :return: str, container id
```

**tag\_and\_push\_image**(self, image, target\_image, insecure=False, force=False):
```
    tag provided image and push it to registry

    :param image: str or ImageName, image id or name
    :param target_image: ImageName, img
    :param insecure: bool, allow connecting to registry over plain http
    :param force: bool, force the tag?
    :return: str, image (reg.com/img:v1)
```

**tag\_image**(self, image, target\_image, force=False):
```
    tag provided image with specified image_name, registry and tag

    :param image: str or ImageName, image to tag
    :param target_image: ImageName, new name for the image
    :param force: bool, force tag the image?
    :return: str, image (reg.om/img:v1)
```

**wait**(self, container\_id):
```
    wait for container to finish the job (may run infinitely)

    :param container_id: str
    :return: int, exit code
```
## Module 'inner'

### Classes
### `class` DockerBuildWorkflow 
    This class defines a workflow for building images:

    1. pull image from registry
    2. tag it properly if needed
    3. obtain source
    4. build image
    5. tag it
    6. push it to registries

#### Instance variables
* build_logs

* builder

* built_image_inspect

* image

* kwargs

* plugin_files

* postbuild_plugins_conf

* postbuild_results

* prebuild_plugins_conf

* prebuild_results

* prepublish_plugins_conf

* pulled_base_images

* repos

* source

* tag_and_push_conf

* target_registries

* target_registries_insecure

#### Methods
**\_\_init\_\_**(self, source, image, parent\_registry=None, target\_registries=None, prebuild\_plugins=None, prepublish\_plugins=None, postbuild\_plugins=None, plugin\_files=None, parent\_registry\_insecure=False, target\_registries\_insecure=False, \*\*kwargs):
```
    :param source: dict, where/how to get source code to put in image
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param target_registries: list of str, list of registries to push image to (might change in future)
    :param prebuild_plugins: dict, arguments for pre-build plugins
    :param prepublish_plugins: dict, arguments for test-build plugins
    :param postbuild_plugins: dict, arguments for post-build plugins
    :param plugin_files: list of str, load plugins also from these files
    :param target_registries_insecure: bool, allow connecting to target registries over plain http
```

**build\_docker\_image**(self):
```
    build docker image

    :return: BuildResults
```
## Module 'build'

### Classes
### `class` InsideBuilder 
    This is expected to run within container

#### Instance variables
* base_image

* base_image_id

* built_image_info

* image

* image_id

* last_logs  
`logs from last operation `

* source

* tasker

#### Methods
**\_\_init\_\_**(self, source, image, tmpdir=None, \*\*kwargs):

**build**(self):
```
    build image inside current environment;
    it's expected this may run within (privileged) docker container

    :return: image string (e.g. fedora-python:34)
```

**get\_base\_image\_info**(self):
```
    query docker about base image

    :return dict
```

**get\_built\_image\_info**(self):
```
    query docker about built image

    :return dict
```

**inspect\_base\_image**(self):
```
    inspect base image

    :return: dict
```

**inspect\_built\_image**(self):
```
    inspect built image

    :return: dict
```
