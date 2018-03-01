## Building Flatpaks

In addition to building docker images from Dockerfiles, atomic-reactor can also build Flatpak OCI images for both runtimes and applications. A Flatpak OCI image is defined by two things: by a [module](https://docs.pagure.org/modularity/docs.html) containing the RPMs to build into the Flatpak, and by information included in a `container.yaml` file.

Building Flatpaks requires, in addition to a Koji installation:

 * A [MBS (module-build-service)](https://pagure.io/fm-orchestrator/) instance set up to build modules in Koji
 * A [PDC (product definition center)](https://github.com/product-definition-center/product-definition-center) instance to store information about the built modules
 * An [ODCS (on demand compose service)](https://pagure.io/odcs/) instance to create repositories for the build modules

A modified version of osbs-box for testing Flatpak building can be found at:

  https://github.com/owtaylor/osbs-box

It sets up everything other than the MBS instance, and contains scripts to create fake module builds for an [example runtime](https://github.com/owtaylor/minimal-runtime), and an [example application](https://github.com/owtaylor/banner).

### container.yaml

To build a flatpak, you need a `container.yaml`. The `compose` section, should have:

``` yaml
compose:
    modules:
    - MODULE_NAME:MODULE_STREAM[/PROFILE]
```

where `PROFILE` defaults to `runtime` for runtimes and `default` for applications. Specifying a
different profile is useful to build additional runtimes from the same module content - for
example, building both a normal runtime and an SDK.

The `flatpak` section of container.yaml contains extra information needed to create the flatpak:

**id**: (required) The ID of the application or runtime

**branch**: (required) The branch of the application or runtime. In many cases, this will match the stream name of the module.

**cleanup-commands**: (optional, runtime only). A shell script that is run after installing all packages.

**command**: (optional, application only). The name of the executable to run to start the application. If not specified, defaults to the first executable found in /usr/bin.

**tags**: (optional, application only). Tags to add to the Flatpak metadata for searching.

**finish-args**: (optional, application only). Arguments to `flatpak build-finish`. (see the flatpak-build-finish man page.) This is a string split on whitespace with shell style quoting.

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
