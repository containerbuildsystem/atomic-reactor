# API

Atomic Reactor has proper python API. You can use it in your scripts or services without invoking shell:

```python
from atomic_reactor.api import build_image_in_privileged_container
response = build_image_in_privileged_container(
    "privileged-buildroot",
    source={
        'provider': 'git',
        'uri': 'https://github.com/TomasTomecek/docker-hello-world.git',
    }
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
