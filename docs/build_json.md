## build.json

If you want to take advantage of _inner_ part logic of Atomic Reactor, you can do that pretty easily. All you need to know, is the structure of json, which is used within build container. Here it is:

```json
{
    "source": {
        "provider": "git",
        "uri": "http://...",
        "dockerfile_path": "django/",
        "provider_params": {
            "git_commit": "devel"
        }
    },
    "image": "my-test-image",
    "parent_registry": "registry.example.com:5000",
    "target_registries": ["registry.example2.com:5000"],
    "prebuild_plugins": [{
        "name": "koji",
        "args": {
            "target": "f22",
            "hub": "http://koji.fedoraproject.org/kojihub",
            "root": "https://kojipkgs.fedoraproject.org/"
        }}, {
            "name": "inject_yum_repo",
            "args": {}
        }
    ]
}
```

 * provider - string, `git` or `path`
 * uri - string, path to git repo with Dockerfile (can be local file reference for `path` provider)
 * dockerfile_path - string, optional, path to dockerfile relative to `uri`
 * provider_params - dict, optional, extra parameters that may be different across providers
  * git_commit - string, allowed for `git` source, git commit to checkout
 * image - string, tag for built image
 * parent_registry - string, optional, registry to pull base image from
 * target_registries - list of strings, optional, registries where built image should be pushed
 * prebuild_plugins - list of dicts, optional
  * list of plugins which are executed prior to build, order _matters_! In this case, first there is generated yum repo for koji f22 tag and then it is injected into dockerfile
 * prepublish_plugins - list of dicts, optional
  * these plugins are executed after the prebuild plugins but before the postbuild plugins
 * postbuild_plugins - list of dicts, optional
  * these plugins are executed last

Atomic Reactor is able to read this build json from various places (see input plugins in source code). There is an argument for command `inside-build` called `--input`. Currently there are 3 available inputs:

 1. `--input path` load build json from arbitrary path (if not specified, it tries to load it from `/run/share/build.json`). You can specify the path like this: `--input-arg path=/my/cool/path/with/build.json`.
 2. `--input env` load it from environment variable: `--input-arg env_name=HERE_IS_THE_JSON`
 3. `--input osv3` import input from OpenShift v3 environment (check github.com/openshift/origin/ for more info)

If the `--input` argument is omitted, and exactly one available input method is detected, Atomic Reactor will use that input method.
