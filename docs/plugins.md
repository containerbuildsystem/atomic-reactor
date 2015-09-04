Plugins
=======

One of the biggest strength of Atomic Reactor is its plugin system.

## Plugin types

There are 5 types of plugins:

1. **Input** — when running Atomic Reactor inside a build container, input plugin fetches build input (it can live in [path](https://github.com/projectatomic/atomic-reactor/blob/master/atomic_reactor/plugins/input_path.py), [environment variable](https://github.com/projectatomic/atomic-reactor/blob/master/atomic_reactor/plugins/input_env.py), ...)
2. **Pre-build** — this plugin is executed after cloning a git repo (so you can edit sources, Dockerfile, ...), prior to build
3. **Pre-publish** — once build finishes, the built image is pushed to registry, but prior to that, pre-publish plugins are executed (time for some simple tests)
4. **Post-build** — these are run when the build is finished and the image was pushed to registries
5. **Exit** — these are run last of all, and will always run even if a previous build step failed

## Plugin configuration

Build plugins are requested and configured via input json: key `prebuild_plugins`, `postbuild_plugins` or `prepublish_plugins`. Each field is an array of following items:

```
{
    "name": "plugin_name",
    "is_allowed_to_fail": false,
    "args": {
        "args1": "value"
    }
}
```

Order is important, because plugins are executed in the order as they are specified (one plugin can use input from another plugin). `args` are directly passed to a plugin in constructor. Any plugin with `is_allowed_to_fail` set to `false` that raises an exception causes the build process to proceed directly to the stage of running exit plugins.


## Input plugins

Input plugin is requested via command line: command `inside-build`, option `--input`. If this option is not given, atomic-reactor tries to autodetect which input plugin to run. If the autodetection fails, atomic-reactor exits and asks you to explicitly specify `--input`. Some input plugins require configuration, e.g. path plugins requires a file path to the file with build json. This is done via argument `--input-arg`. It has special syntax: `--input-arg key=value`.

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

### osv3 input plugin

Loads build configuration from environment variables provided by OpenShift v3. Sample usage:

```
--input osv3
```

### Configuration substitution

You may substitute configuration in your provided build json. This is really handy if you have a template with build json and don't want to change it yourself.

All you need to do to accomplish this is to pass argument `--substitute` with value:
 * `argument_name=argument_value`
 * `plugin_type.plugin_name.argument_name=argument_value`

E.g. `--substitute image=my-nice-image --substitute prebuild_plugins.koji.target=f22`.

