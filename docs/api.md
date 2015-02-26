# API

dock has proper python API. You can use it in your scripts or services without invoking shell:

```python
from dock.api import build_image_in_privileged_container
response = build_image_in_privileged_container(
    "privileged-buildroot",
    git_url="https://github.com/TomasTomecek/docker-hello-world.git",
    image="dock-test-image",
)
```


## Module 'api'
Python API for dock. This is the official way of interacting with dock.

### Functions
**build\_image\_here**(git\_url, image, git\_dockerfile\_path=None, git\_commit=None, parent\_registry=None, target\_registries=None, \*\*kwargs):
```
    build image from provided dockerfile (specified as git url) in current environment

    :param git_url: str, URL to git repo
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param git_dockerfile_path: str, path to dockerfile within git repo (if not in root)
    :param git_commit: str, git commit to check out
    :param parent_registry: str, registry to pull base image from
    :param target_registries: list of str, list of registries to push image to (might change in future)

    :return: BuildResults
```

**build\_image\_in\_privileged\_container**(build\_image, git\_url, image, git\_dockerfile\_path=None, git\_commit=None, parent\_registry=None, target\_registries=None, push\_buildroot\_to=None, \*\*kwargs):
```
    build image from provided dockerfile (specified as git url) in privileged image

    :param build_image: str, image where target image should be built
    :param git_url: str, URL to git repo
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param git_dockerfile_path: str, path to dockerfile within git repo (if not in root)
    :param git_commit: str, git commit to check out
    :param parent_registry: str, registry to pull base image from
    :param target_registries: list of str, list of registries to push image to (might change in future)
    :param push_buildroot_to: str, repository where buildroot should be pushed

    :return: BuildResults
```

**build\_image\_using\_hosts\_docker**(build\_image, git\_url, image, git\_dockerfile\_path=None, git\_commit=None, parent\_registry=None, target\_registries=None, push\_buildroot\_to=None, \*\*kwargs):
```
    build image from provided dockerfile (specified as git url) in container
    using docker from host

    :param build_image: str, image where target image should be built
    :param git_url: str, URL to git repo
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param git_dockerfile_path: str, path to dockerfile within git repo (if not in root)
    :param git_commit: str, git commit to check out
    :param parent_registry: str, registry to pull base image from
    :param target_registries: list of str, list of registries to push image to (might change in future)
    :param push_buildroot_to: str, repository where buildroot should be pushed

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
    :param image: str, repository[:tag]
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
    :param image: str, repository[:tag]
    :param stream: bool, True returns generator, False returns str
    :param use_cache: bool, True if you want to use cache
    :param remove_im: bool, remove intermediate containers produced during docker build
    :return: generator
```

**commit\_container**(self, container\_id, repository=None, message=None):
```
    create image from provided container

    :param container_id: str
    :param repository: str (repo/image_name)
    :param message: str
    :return: image_id
```

**get\_image\_info\_by\_image\_id**(self, image\_id):
```
    using `docker images`, provide information about an image

    :param image_id: str, hash of image to get info
    :return: str or None
```

**get\_image\_info\_by\_image\_name**(self, image\_name, reg\_uri='', tag=None):
```
    using `docker images`, provide information about an image

    :param image_name: str, name of image (without tag!)
    :param reg_uri: str, optional registry
    :return: list of dicts
```

**image\_exists**(self, image\_id):
```
    does provided image exists?

    :param image_id: str
    :return: True if exists, False if not
```

**inspect\_image**(self, image\_id):
```
    return detailed metadata about provided image (see 'man docker-inspect')

    :param image_id: str
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

**pull\_image**(self, image\_name, reg\_uri, tag=''):
```
    pull provided image from registry

    :param image_name: str, image name
    :param reg_uri: str, reg.com
    :param tag: str, v1
    :return: str, image (reg.om/img:v1)
```

**push\_image**(self, image):
```
    push provided image to registry

    :param image: str
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

    :param image_id: str
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

    :param image: str
    :param command: str
    :param create_kwargs: dict, kwargs for docker.create_container
    :param start_kwargs: dict, kwargs for docker.start
    :return: str, container id
```

**tag\_and\_push\_image**(self, image, target\_image\_name, reg\_uri='', tag=''):
```
    tag provided image and push it to registry

    :param image: str (reg.com/img:v1)
    :param target_image_name: str, img
    :param reg_uri: str, reg.com
    :param tag: str, v1
    :return: str, image (reg.om/img:v1)
```

**tag\_image**(self, image, target\_image\_name, reg\_uri='', tag='', force=False):
```
    tag provided image with specified image_name, registry and tag

    :param image: str (reg.com/img:v1)
    :param target_image_name: str, img
    :param reg_uri: str, reg.com
    :param tag: str, v1
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
    3. clone git repo
    4. build image
    5. tag it
    6. push it to registries

#### Instance variables
* build_logs

* builder

* git_commit

* git_dockerfile_path

* git_url

* image

* kwargs

* parent_registry

* plugin_files

* postbuild_plugins_conf

* postbuild_results

* prebuild_plugins_conf

* prebuild_results

* prepublish_plugins_conf

* repos

* target_registries

#### Methods
**\_\_init\_\_**(self, git\_url, image, git\_dockerfile\_path=None, git\_commit=None, parent\_registry=None, target\_registries=None, prebuild\_plugins=None, prepublish\_plugins=None, postbuild\_plugins=None, plugin\_files=None, \*\*kwargs):
```
    :param git_url: str, URL to git repo
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param git_dockerfile_path: str, path to dockerfile within git repo (if not in root)
    :param git_commit: str, git commit to check out
    :param parent_registry: str, registry to pull base image from
    :param target_registries: list of str, list of registries to push image to (might change in future)
    :param prebuild_plugins: dict, arguments for pre-build plugins
    :param prepublish_plugins: dict, arguments for test-build plugins
    :param postbuild_plugins: dict, arguments for post-build plugins
    :param plugin_files: list of str, load plugins also from these files
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
* base_image_id

* base_image_info

* built_image_info

* df_base_image

* git_commit

* git_dockerfile_path

* git_path

* git_url

* image

* image_id

* last_logs  
`logs from last operation `

* tasker

#### Methods
**\_\_init\_\_**(self, git\_url, image, git\_dockerfile\_path=None, git\_commit=None, tmpdir=None, \*\*kwargs):

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

**pull\_base\_image**(self, source\_registry):
```
    pull base image

    :param source_registry: str, registry to pull from
    :return:
```

**push\_built\_image**(self, registry):
```
    push built image to provided registry

    :param registry: str
    :return: str, image
```
