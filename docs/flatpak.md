# Building Flatpaks

In addition to building docker images from Dockerfiles, atomic-reactor can also
build Flatpak OCI images for both runtimes and applications. A Flatpak OCI image
is defined by two things: by a [module][] containing the RPMs to build into the
Flatpak, and by information included in a `container.yaml` file.

Building Flatpaks requires, in addition to a Koji installation:

- An [MBS][] (Module-Build-Service) instance set up to build modules in Koji
- An [ODCS][] (On Demand Compose Service) instance to create repositories for
  the build modules

A [modified version][] of osbs-box, for testing Flatpak building, is available.
It sets up everything other than the MBS instance, and contains scripts to
create fake module builds for an [example runtime][], and an [example
application][].

## container.yaml

To build a flatpak, you need a `container.yaml`. The `compose` section should
contain

``` yaml
compose:
    modules:
    - MODULE_NAME:MODULE_STREAM[/PROFILE]
```

where `PROFILE` defaults to `runtime` for runtimes and `default` for
applications. Specifying a different profile is useful to build additional
runtimes from the same module content ― for example, building both a normal
runtime and an SDK.

The `flatpak` section of container.yaml contains extra information needed to
create the flatpak:

- **id**: (required) The ID of the application or runtime
- **name**: (optional) `name` label in generated Dockerfile. Used for the
  repository when pushing to a registry. Defaults to the module name
- **component**: (optional) `com.redhat.component` label in generated
  Dockerfile. Used to name the build when uploading to Koji. Defaults to the
  module name
- **base_image**: (optional) Image to use when creating filesystem; also will
  be recorded as the parent image of the output image. This overrides the
  `flatpak: base_image` setting in the reactor config
- **branch**: (required) The branch of the application or runtime. In many
  cases, this will match the stream name of the module
- **cleanup-commands**: (optional, runtime only) A shell script that is run
  after installing all packages
- **command**: (optional, application only) The name of the executable to run
  to start the application. If not specified, defaults to the first executable
  found in `/usr/bin`
- **labels**: (optional) A map defining additional labels to be added to the
  resulting image
- **tags**: (optional, application only) Tags to add to the Flatpak metadata
  for searching
- **finish-args**: (optional, application only) Arguments to `flatpak
  build-finish` (see the flatpak-build-finish man page) This is a string, split
  on whitespace, with shell-style quoting

### container.yaml examples

Runtime:

```yaml
compose:
    modules:
    - flatpak-runtime:f28/runtime
flatpak:
    id: org.fedoraproject.Platform
    branch: f28
    cleanup-commands: |
        touch -d @0 /usr/share/fonts
        touch -d @0 /usr/share/fonts/*
        fc-cache -fs
```

Application:

```yaml
compose:
    modules:
    - eog:f28
flatpak:
    id: org.gnome.eog
    branch: stable
    command: eog
    labels:
        maintainer: susan@example.com
    tags: ["Viewer"]
    finish-args: >
        --filesystem=host
        --share=ipc
        --socket=x11
        --socket=wayland
        --socket=session-bus
        --filesystem=~/.config/dconf:ro
        --filesystem=xdg-run/dconf
        --talk-name=ca.desrt.dconf
        --env=DCONF_USER_CONFIG_DIR=.config/dconf
```

[module]: https://docs.pagure.org/modularity/docs.html
[MBS]: https://pagure.io/fm-orchestrator
[ODCS]: https://pagure.io/odcs
[modified version]: https://github.com/owtaylor/osbs-box
[example runtime]: https://github.com/owtaylor/minimal-runtime
[example application]: https://github.com/owtaylor/banner
