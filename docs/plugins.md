Plugins
=======

One of the biggest strength of dock is its plugin system.

## Plugin types

There are 4 types of plugins:

1. **Input** — when running dock inside a build container, input plugin fetches build input (it can live in [path](https://github.com/DBuildService/dock/blob/master/dock/plugins/input_path.py), [environment variable](https://github.com/DBuildService/dock/blob/master/dock/plugins/input_env.py), ...)
2. **Pre-build** — this plugin is executed after cloning a git repo (so you can edit sources, Dockerfile, ...), prior to build
3. **Pre-publish** — once build finishes, the built image is pushed to registry, but prior to that, pre-publish plugins are executed (time for some simple tests)
4. **Post-build** — these are run as a last thing of build process (when build is finished and image was pushed to registries)

## Plugin configuration

Build plugins are requested and configured via input json: key `prebuild_plugins`, `postbuild_plugins` or `prepublish_plugins`. Each field is an array of following items:

```
{
    "name": "plugin_name",
    "can_fail": false,
    "args": {
        "args1": "value"
    }
}
```

Order is important, because plugins are executed in the order as they are specified (one plugin can use input from another plugin). `args` are directly passed to a plugin in constructor. If `can_fail` is set to `false`, once the plugin raises an exception, build process is halted.


Input plugin is requested via command line: command `inside-build`, option `--input`.

