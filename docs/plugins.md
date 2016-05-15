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


## Default workflow

The plugins listed as part of the production workflow in the default prod_inner.json are listed below in the order they are used.

### Pre-build plugins

These are run after 'git clone' is used to fetch the git repository content containing the Dockerfile.

 * **is_autorebuild**
   * Status: not yet enabled (chain rebuilds)
   * Several plugins have specific duties to perform only in the case of automated rebuilds. This plugin figures out whether this OpenShift Build is an explicit build requested by a developer (via Koji), or whether it is a build triggered by a change in the parent layer.
 * **stop_autorebulid_if_disabled**
   * Status: not yet enabled (chain rebuilds)
   * Based on the result of is_autorebuild, this plugin is for stopping automated builds from proceeding if the image owner has opted out of automated rebuilds.
 * **pull_base_image**
   * Status: enabled
   * The image named in the FROM line of the Dockerfile is pulled and its docker image ID noted.
 * **bump_release**
   * Status: not yet enabled (chain rebuilds)
   * In order to support automated rebuilds, this plugin is tasked with incrementing the 'Release' label in the Dockerfile.
 * **add_labels_in_dockerfile**
   * Status: enabled
   * Labels that are specified in the builder configuration, such as the vendor name, distribution scope, and authoritative registry, are added to the Dockerfile using LABEL. This plugin also adds automatic labels such as the build date, architecture, build host, and git reference.
 * **change_from_in_dockerfile**
   * Status: enabled
   * The FROM line in the Dockerfile is changed so that it references the specific docker image ID we pulled earlier.
 * **add_dockerfile**
   * Status: enabled
   * The Dockerfile used to build the image has a line added to ADD itself into the built image.
 * **distgit_fetch_artefacts**
   * Status: enabled
   * This plugin runs a command (e.g. 'fedpkg sources') to fetch any necessary files from the lookaside cache.
 * **koji**
   * Status: enabled
   * Based on the Koji build target for this build, a yum repo file is created so that packages can be installed from that target in Koji.
 * **add_yum_repo_by_url**
   * Status: enabled
   * If the developer requested a specific yum repo URL for this build, this plugin fetches the yum repo file from that URL.
 * **inject_yum_repo**
   * Status: enabled
   * The yum repo file or files created by the koji and add_yum_repo_by_url plugins are injected into the Dockerfile with ADD, and cleaned up with 'RUN rm'. When the built image has its new layers squashed later, the yum repo files will not appear in the content.

After the pre-build plugins have finished, 'docker build' is started.
 
### Pre-publish and post-build plugins

These are run after 'docker build' has finished.

 * **squash**
   * Status: enabled
   * Layers created as part of the docker build process are squashed together into a single layer. The output of this plugin is a 'docker save'-style tarball.
 * **compress**
   * Status: enabled
   * The 'docker save' output is compressed using gzip.
 * **tag_by_labels**
   * Status: enabled
   * The Name, Version, and Release labels in the Dockerfile are used to create tags to be applied to the image:
     * ${Name}:${Version}-${Release}
     * ${Name}:${Version}
     * ${Name}:latest
 * **tag_and_push**
   * Status: enabled for V2
   * The tags are applied to the image in the docker engine and pushed to configured registries.
 * **pulp_push**
   * Status: enabled for V1
   * This plugin gets the built image into the Pulp server in such a way that they will be available (through Crane) via the Docker Registry HTTP V1 API. The 'docker save' output is uploaded to Pulp, the tags are set on the uploaded Pulp content, and the content is published to Crane.
 * **pulp_sync**
   * Status: enabled for V2
   * This is the V2 equivalent of pulp_push. Having previously pushed the built image to a docker-distribution V2 registry, this plugin tells the Pulp server to sync that content in. After publishing the content to Crane, it is now available via the Docker Registry HTTP V2 API.
 * **all_rpm_packages**
   * Status: enabled
   * A container is started to run 'rpm -qa' inside the built image in order to gather information needed for the Content Generator import into Koji later.
 * **import_image**
   * Status: not yet enabled (chain rebuilds)
   * OpenShift is asked to import image tags from Crane into the ImageStream object it maintains representing the image we just built. This step is what triggers rebuilds of dependent images.

### Exit plugins

These are run at the end of the build, even for failed builds.

 * **koji_promote**
   * Status: enabled
   * The 'docker save' output, build logs, and metadata are imported into Koji to create a Koji Build object.
 * **store_metadata_in_osv3**
   * Status: enabled
   * The OpenShift Build object is annotated with information about the build, such as the Koji Build ID, built docker image ID, parent docker image ID, etc.
 * **remove_built_image**
   * Status: enabled
   * The built image is removed from the docker engine.
 * **sendmail**
   * Status: not yet enabled (chain rebuilds)
   * If this build was triggered by a chain in a parent layer, rather than having been explicitly requested by a developer, email is sent to the image owner(s) about the success or failure of the build.
