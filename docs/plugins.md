Plugins
=======

One of the biggest strength of Atomic Reactor is its plugin system.

## Plugin types

There are 4 types of plugins:

1. **Input** — when running Atomic Reactor inside a build container, input plugin fetches build input (it can live in [path](https://github.com/DBuildService/atomic-reactor/blob/master/atomic_reactor/plugins/input_path.py), [environment variable](https://github.com/DBuildService/atomic-reactor/blob/master/atomic_reactor/plugins/input_env.py), ...)
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


## Input plugins

Input plugin is requested via command line: command `inside-build`, option `--input`. Some input plugins require configuration, e.g. path plugins requires a file path to the file with build json. This is done via argument `--input-arg`. It has special syntax: `--input-arg key=value`.

### path input plugin

This input plugin reads specified file from filesystem and uses it as build json. Sample usage:
```
--input path --input-arg path=path/to/the/build.json
```

### env input plugin

Loads specified environment variable as build json. Sample usage:
```
--input env --input-arg env_name=MY_BUILD_JSON
```

### Configuration substitution

You may substitute configuration in your provided build json. This is really handy if you have a template with build json and don't want to change it yourself.

All you need to do to accomplish this is to pass argument `--substitute` with value:
 * `argument_name=argument_value`
 * `plugin_type.plugin_name.argument_name=argument_value`

E.g. `--substitute image=my-nice-image --substitute prebuild_plugins.koji.target=f22`.

