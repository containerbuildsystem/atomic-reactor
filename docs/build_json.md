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
}
```

 * git_url - string, path to git repo with Dockerfile
 * image - string, tag for built image
 * git_dockerfile_path - string, optional, path to dockerfile within git repo
 * git_commit - string, optional, git commit to checkout
 * parent_registry - string, optional, registry to pull base image from
 * target_registries - list of strings, optional, registries where built image should be pushed
 * prebuild_plugins - list of dicts
  * list of plugins which are executed prior to build, order _matters_! In this case, first there is generated yum repo for koji f22 tag and then it is injected into dockerfile

dock is able to read this build json from various places (see input plugins in source code). There is argument for command `inside-build` called `--input`. Currently there are 3 available inputs:

 1. `--input path` load build json from arbitrary path (if not specified, it tries to load it from `/run/share/build.json`). You can specify the path like this: `--input-arg path=/my/cool/path/with/build.json`.
 2. `--input env` load it from environment variable: `--input-arg env_name=HERE_IS_THE_JSON`
 3. `--input osv3` import input from OpenShift v3 environment (check github.com/openshift/origin/ for more info)


