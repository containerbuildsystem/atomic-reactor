# Plugins

One of the biggest strength of Atomic Reactor is its plugin system.

## Plugin types

There are 6 types of plugins

1. **Input**: When running Atomic Reactor inside a build container, the input
   plugin fetches the build input (it can live in [path][], [environment
   variable][], etc.)
1. **Pre-build**: This plugin is executed after cloning a git repo (so you can
   edit sources, Dockerfile, etc.), prior to build
1. **Buildstep**: This plugin does the actual building
1. **Pre-publish**: Once the build finishes, the built image is pushed to the
   registry, but prior to that, pre-publish plugins are executed (time for some
   simple tests)
1. **Post-build**: These are run after the build is finished and the image was
   pushed to the registry
1. **Exit**: These are run last of all, and will always run even if a previous
   build step failed

## Plugin configuration

Build plugins are requested and configured via input JSON key

- `prebuild_plugins`
- `buildstep_plugins`
- `prepublish_plugins`
- `postbuild_plugins`
- `exit_plugins`

Each field is an array of following items:

```json
{
  "name": "plugin_name",
  "is_allowed_to_fail": false,
  "args": {
    "args1": "value"
  },
  "required": true
}
```

Order is important, because plugins are executed in the order as they are
specified (one plugin can use input from another plugin). `args` are directly
passed to a plugin in constructor. Any plugin with `is_allowed_to_fail` set to
`false` that raises an exception causes the build process to proceed directly to
the stage of running exit plugins.

The optional `required` key, which defaults to `true`, specifies whether this
plugin is required for a successful build. If the plugin is not available and
`required` is set to `false`, the build will not fail. However if the plugin is
available and that plugin sets `is_allowed_to_fail` to `false`, the plugin can
still cause the build to fail (exit plugins are run immediately). This is useful
for validation plugins not present in older builder images.

## Input plugins

Input plugin is requested via command line: command `inside-build`, option
`--input`. If this option is not given, atomic-reactor tries to autodetect which
input plugin to run. If the autodetection fails, atomic-reactor exits and asks
you to explicitly specify `--input`. Some input plugins require configuration,
e.g. path plugins requires a file path to the file with build json. This is done
via argument `--input-arg`. It has special syntax: `--input-arg key=value`.

### 'path' input plugin

This input plugin reads specified file from filesystem and uses it as build
json.

Sample usage

```bash
--input path --input-arg path=path/to/the/build.json
```

### 'env' input plugin

Loads specified environment variable as build json.

Sample usage

```bash
--input env --input-arg env_name=MY_BUILD_JSON
```

### 'osv3' input plugin

Loads build configuration from environment variables provided by OpenShift v3.

Sample usage

```bash
--input osv3
```

### Configuration substitution

You may substitute configuration in your provided build json. This is really
handy if you have a template with build json and don't want to change it
yourself.

All you need to do to accomplish this is to pass argument `--substitute` with
value

- `argument_name=argument_value`
- `plugin_type.plugin_name.argument_name=argument_value`

E.g.

- `--substitute image=my-nice-image`
- `--substitute prebuild_plugins.koji.target=f22`

## Buidstep plugins

Unlike other plugins, buildstep plugins have some differences.

The `required` and `is_allowed_to_fail` properties will be set to `false` at
runtime.

Buildstep plugins are run in order, and if one plugin successfully completes or
fails, the remaining buildstep plugins will not be attempted.

The run() method must raise `InappropriateBuildStepError` to indicate the next
buildstep plugin in the list should be attempted.

## Default workflow

The plugins listed as part of the production workflow in the default
prod_inner.json are listed below in the order they are used.

### Pre-build plugins

These are run after 'git clone' is used to fetch the git repository content
containing the Dockerfile.

- **reactor_config**
  - Status: Not yet enabled (multi-cluster)
  - Parse, validate, and make available to other plugins the atomic-reactor
    configuration file
- **add_filesystem**
  - Status: Enabled
  - If FROM value is "koji/image-build", an image-build koji task is initiated
    to create the underlying filesystem base image. Once task is completed, the
    built filesystem image is imported into docker and its ID is used as the
    FROM value
- **check_base_image**
  - Status: Enabled
  - The image named in the FROM line of the Dockerfile is checked
- **bump_release**
  - Status: Enabled
  - In order to support automated rebuilds, this plugin is tasked with
    incrementing the 'release' label in the Dockerfile
- **add_labels_in_dockerfile**
  - Status: Enabled
  - Labels that are specified in the builder configuration, such as the vendor
    name, distribution scope, and authoritative registry, are added to the
    Dockerfile using LABEL. This plugin also adds automatic labels such as the
    build date, architecture, info url, and git reference.
- **change_from_in_dockerfile**
  - Status: Enabled
  - The FROM line in the Dockerfile is changed so that it references the
    specific docker image ID we pulled/imported earlier
- **check_user_settings**
  - Status: Enabled
  - Checks user settings (like container.yaml, Dockerfile) on early phase to
    fail early and save resources
- **add_help**
  - Status: Enabled
  - Markdown help page is converted to a man page and ADD'ed into the built
    image in order to show a correct page when `atomic help` is called.
- **add_image_content_manifest**
  - Status: Enabled
  - creates metadata_{current_layer_index}.json with Cachito ICM, content_sets,
    and other metadata, and ADDs it to the built image
- **add_dockerfile**
  - Status: Enabled
  - The Dockerfile used to build the image has a line added to COPY itself into
    the built image
- **distgit_fetch_artefacts**
  - Status: Enabled
  - This plugin runs a command (e.g. `fedpkg sources`) to fetch any necessary
    files from the lookaside cache
- **koji**
  - Status: Enabled
  - Based on the Koji build target for this build, a yum repo file is created so
    that packages can be installed from that target in Koji
- **koji_parent**
  - Status: Enabled
  - Verified parent image has a corresponding Koji build
- **add_yum_repo_by_url**
  - Status: Enabled
  - If the developer requested a specific yum repo URL for this build, this
    plugin fetches the yum repo file from that URL
- **inject_yum_repo**
  - Status: Enabled
  - The yum repo file or files created by the `koji` and `add_yum_repo_by_url`
    plugins are injected into the Dockerfile with ADD, and cleaned up with
    `RUN rm`. When the built image has its new layers squashed later, the yum
    repo files will not appear in the content
- **distribution_scope**
  - Status: Enabled
  - The distribution-scope image labels for the parent and the current image are
    compared and invalid combinations cause the build to fail
- **fetch_maven_artifacts**
  - Status: Enabled
  - Download artifacts from either a koji build or directly from a URL
- **inject_parent_image**
  - Status: Enabled
  - Overwrite parent image image reference
- **pin_operator_digest**
  - Status: Enabled
  - Replace image pullspecs in operator manifest files (tag => digest, other
    replacements based on OSBS configuration)

### Buildstep plugins

These are run after we have everything ready for build

- **docker_api**
  - Status: Enabled
  - Builds image inside current environment, using docker api
- **imagebuilder**
  - Status: Enabled
  - Builds image inside current environment, using [imagebuilder][]
- **buildah_bud**
  - Status: Not yet enabled
  - Builds image inside current environment, using [buildah bud][]
- **orchestrate_build**
  - Status: Enabled
  - Builds image in remote environment
- **source_container**
  - Status: Not yet enabled
  - Builds source image using [BuildSourceImage][] tool

### Pre-publish and post-build plugins

These are run after the buildstep plugin has successfully finished

- **squash**
  - Status: Enabled
  - Layers created as part of the docker build process are squashed together
    into a single layer. The output of this plugin is a `docker save`-style
    tarball.
- **compress**
  - Status: Enabled
  - The `docker save` output is compressed using gzip.
- **tag_from_config**
  - Status: Enabled
  - Tags defined in file 'additional-tags' will be applied to the image:
    - `${name}:${additional-tag1}`
    - `${name}:${additional-tag2}`
    - `${name}:${additional-tag3}`
    - etc.
- **tag_and_push**
  - Status: Enabled for V2
  - The tags are applied to the image in the docker engine and pushed to
    configured registries
- **all_rpm_packages**
  - Status: Enabled
  - A container is started to run `rpm -qa` inside the built image in order to
    gather information needed for the Content Generator import into Koji later
- **import_image**
  - Status: Not yet enabled (chain rebuilds)
  - OpenShift is asked to import image tags from Crane into the ImageStream
    object it maintains representing the image we just built. This step is what
    triggers rebuilds of dependent images
- **export_operator_manifests**
  - Status: Enabled
  - When specified through the 'com.redhat.delivery.appregistry' Dockerfile
    label, the operator manifests under the '/manifests' directory are extracted
    from the built image as a zip archive
- **koji_upload**
  - Status: Enabled
  - The `docker save` output, build logs, and operator manifests are uploaded
     to Koji. The metadata is returned to be used by the store_metadata_osv3
     plugin. That plugin will use a ConfigMap object to store it for the
     orchestrator to retrieve it. It will replace **koji_promote** when enabled

### Exit plugins

These are run at the end of the build, even for failed builds.

- **koji_promote**
  - Status: Enabled
  - The `docker save` output, build logs, and metadata are imported into Koji to
    create a Koji Build object
- **koji_import**
  - Status: Disabled
  - Aggregates output of **koji_upload** for each worker build to create a Koji
    Build object. It will replace **koji_promote** when enabled
- **store_metadata**
  - Status: Enabled
  - The OpenShift Build object is annotated with information about the build,
    such as the Koji Build ID, built docker image ID, parent docker image ID,
    etc.
- **koji_tag_build**
  - Status: Enabled
  - Tags the imported Koji build based on a given target
- **remove_built_image**
  - Status: Enabled
  - The built image is removed from the docker engine
- **sendmail**
  - Status: Not yet enabled (chain rebuilds)
  - If this build was triggered by a chain in a parent layer, rather than having
    been explicitly requested by a developer, email is sent to the image
    owner(s) about the success or failure of the build

[path]: ../atomic_reactor/plugins/input_path.py
[environment variable]: ../atomic_reactor/plugins/input_env.py
[imagebuilder]: https://github.com/openshift/imagebuilder
[buildah bud]: https://github.com/containers/buildah/blob/master/docs/buildah-bud.md
[BuildSourceImage]: https://github.com/containers/BuildSourceImage
