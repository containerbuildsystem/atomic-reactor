## Building Flatpaks

In addition to building docker images from Dockerfiles, atomic-reactor can also build Flatpak OCI images for both runtimes and applications. A Flatpak OCI image is defined by two things: by a flatpak.json file that contains metadata data about how to build the Flaptak, and a [module](https://docs.pagure.org/modularity/docs.html) containing the RPMs to build into the Flatpak.

### Example build.json

```json
{
    "source": {
        "provider": "git",
        "uri": "git://pkgs.fedoraproject.org/modules/flatpak-runtime"
    },
    "image": "org.fedoraproject.platform",
    "prebuild_plugins": [
        { "name": "flatpak_create_dockerfile",
          "args": {
              "module_name": "flatpak-runtime",
              "module_stream": "f26",
              "base_image": "registry.fedoraproject.org/fedora:26",
              "pdc_url": "https://pdc.fedoraproject.org/rest_api/v1",
              "pdc_insecure": false,
              "compose_url": "https://people.redhat.com/otaylor/modularity/composes/{name}-{stream}-{version}/"
          }
        },
        { "name": "pull_base_image",
          "args": {
              "parent_registry": "registry.fedoraproject.org"
          }
        },
        { "name": "inject_yum_repo",
          "args": {}
        }
    ],
    "prepublish_plugins": [
        { "name": "flatpak_create_oci" }
    ],
    "postbuild_plugins": [
        { "name": "tag_and_push",
                "args": {
                        "registries":
                                { "localhost:5000":
                                  "insecure": true
                                }
                        }
        }
    ]
}

```

### Dependencies

To build flatpaks that actually run currently requires:

* A version of the docker registry [modified to support OCI Images](https://github.com/docker/distribution/pull/2076)
* A version of Skopeo modified to [support pushing OCI images to the docker registry without conversion](https://github.com/projectatomic/skopeo/issues/369)

These are mock'ed for 'make test' - 'make test' only requires binaries that are part of Fedora 26.

### compose URL

Modules build in Fedora koji are currently not built into yum repositories, though this is [planned](https://pagure.io/odcs). To build a Flatpak against a module thus requires you to manually build a yum repository ([flatpak-module-tools] (https://pagure.io/flatpak-module-tools) contains `flatpak-module compose`), put it somewhere publically accessible by HTTP, and provide it as the `compose_url` argument to the `flatpak_create_dockerfile` plugin.

### flatpak.json

The flatpak.json file is inspired by the JSON manifest input to [flatpak builder](http://docs.flatpak.org/en/latest/flatpak-builder.html), but
without the actual build instructions. The flaptak.json for a runtime and for a application are somewhat different.

Runtime:

```json
{
        "runtime": "org.fedoraproject.Platform",
        "runtime-version": "26",
        "sdk": "org.fedoraproject.Sdk",
        "cleanup-commands": [ "touch -d @0 /usr/share/fonts",
                              "touch -d @0 /usr/share/fonts/*",
                              "fc-cache -fs"
                            ]
}

```

Application:

```json
{
        "id": "org.gnome.eog",
        "version": "3.20.0-2.fc26",
        "runtime": "org.fedoraproject.Platform",
        "runtime-version": "26",
        "sdk": "org.fedoraproject.Sdk",
        "command": "eog",
        "tags": ["Viewer"],
        "finish-args": ["--filesystem=host",
                        "--share=ipc",
                        "--socket=x11",
                        "--socket=wayland",
                        "--socket=session-bus",
                        "--filesystem=~/.config/dconf:ro",
                        "--filesystem=xdg-run/dconf",
                        "--talk-name=ca.desrt.dconf",
                        "--env=DCONF_USER_CONFIG_DIR=.config/dconf"]
}
```
