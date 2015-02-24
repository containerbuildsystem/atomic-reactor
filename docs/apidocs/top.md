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


