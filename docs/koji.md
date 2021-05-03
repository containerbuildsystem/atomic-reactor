# Koji Integration

## Plugins

These plugins provide integration with [Koji][]

- pre-build
  - `add_filesystem`
  - `koji`
  - `fetch_maven_artifacts`
  - `inject_parent_image`
- post-build
  - `koji_upload`
- exit
  - `koji_import`
  - `koji_tag_build`

### Pre-build

- `add_filesystem`: Provides special handling for images with `FROM
  koji/image-build`. For these images it creates a Koji task to create a
  installed filesystem archive as [described here][]
- `koji`: Creates a yum repofile to allow instructions in the Dockerfile to have
  access to RPMs available from the appropriate Koji tag
- `koji_parent`: Determines the expected NVR (Name-Version-Release) of the Koji
  build for the parent image. It waits for a given amount if build does not
  exist yet
- `fetch_maven_artifacts`: Uses configuration files to download external maven
  artifacts. One of these sources can be a Koji build
- `fetch_sources`: Determines and pulls sources which should be added into
  source container image, based on provided koji build N-V-R/ID
- `inject_parent_image`: Overwrites the parent image to be used based on a given
  Koji build

### Post-build

- `koji_upload`: Runs in the worker build to upload platform specific
  information to koji, and capture platform specifc metadata.

### Exit

- `koji_import`: Runs as an exit plugin in the orchestrator build to gather the
  platform specific metadata from each worker build and combine into a single
  build to be imported into Koji via the Koji Content Generator [API][].

  When importing a build using the Content Generator API, metadata to describe
  the build is generated. This follows the Koji metadata format
  [specification][] but in some cases content generators are free to choose how
  metadata is expressed.

  Each build creates a single output archive, in the Combined Image JSON +
  Filesystem Changeset [format][].

- `koji_tag_build`: Used to tag the imported koji build based on a
  target. Refer to ["koji tags and targets"][]

## Type-specific metadata

### Build metadata

For atomic-reactor container image builds the `image` type is used, and so
type-specific information is placed into the `build.extra.image` map. Note that
this is a legacy type in koji and may be changed to use
`build.extra.typeinfo.image`. Clients fetching such data should first look for
it within `build.extra.typeinfo.image` and fall back to `build.extra.image` when
the former is not available.

Data which is placed here includes

- `build.extra.image.autorebuild` (boolean): `true` if this build was triggered
  automatically; `false` otherwise
- `build.extra.image.triggered_after_koji_task` (int): Only defined for
  autorebuilds, specifies original Koji task ID for autorebuild
- `build.extra.image.isolated` (boolean): `true` if this build was an isolated
  build; `false` otherwise
- `build.extra.image.help` (str or null): Filename of the markdown help file
  in the repository if this build has a markdown help converted to man page;
  null otherwise
- `build.extra.container_koji_task_id` (int): Koji task ID which created the
  BuildConfig for this OpenShift Build ― note that this location is technically
  incorrect but remains as-is for compatibility with existing software
- `build.extra.filesystem_koji_task_id` (int): Koji task ID which atomic-reactor
  created in order to generate the initial layer of the image (for `FROM
  koji/image-build` images) ― note that this location is technically incorrect
  but remains as-is for compatibility with existing software
- `build.extra.image.media_types` (str list): Container image media types for
  which this image is available, where "application/json" is for a Docker
  Registry HTTP API V1 image; currently this key is only set when Pulp
  integration is enabled
- `build.extra.image.operator_manifests` (map): Operator bundle images metadata
- `build.extra.image.operator_manifests.custom_csv_modifications_applied` (boolean):
  indicates if custom user modifications were done to operator bundle images metadata
- `build.extra.image.operator_manifests.related_images` (map): Metadata about
  related_images in operator bundle
- `build.extra.image.operator_manifests.related_images.pullspecs` (map list):
  list of used pullspecs. Map keys: `original` - original pullspec value in
  CSV; `new` - new pullspec replaces by OSBS; `pinned` - boolean if pullspec
  digest was pinned by OSBS
- `build.extra.image.operator_manifests.related_images.created_by_osbs`
  (boolean): True if `relatedImages` section in CSV file was created by OSBS
- `build.extra.image.parent_build_id` (int): Koji build id of the parent image,
  if found
- `build.extra.image.parent_image_builds` (map of maps): Keys are parent images
  given in the Dockerfile, which are regular images and
  not 'scratch' or custom image ('koji/image-build), and includes registry,
  organization and tag, its value being a map representing the Koji
  build (if found) for that image, with keys `id` (int) and `nvr` (str).
- `build.extra.image.parent_images` (str list): All parent images given in the
  Dockerfile, in the same order, and unmodified.
- `build.extra.image.index` (map): Information about the manifest list
- `build.extra.image.flatpak` (boolean): `true` if this image is a Flatpak
- `build.extra.image.modules` (boolean, currently Flatpak-only): The [modules][]
  that provide the packages for this image, resolved to `NAME-STREAM-VERSION`.
  This will include the resolved versions of the modules in `source_modules`,
  and the dependencies of those modules.
- `build.extra.image.source_modules` (boolean, currently Flatpak-only): The
  modules that were specified as input to the build process.
- `build.extra.image.odcs` (map): Information about ODCS
- `build.extra.image.odcs.compose_ids` (int list): List of each ODCS compose
  used
- `build.extra.image.odcs.signing_intent` (str): Final signing intent of the
  ODCS composes after adjusting for CLI parameter
- `build.extra.image.odcs.signing_intent_overridden` (boolean): Whether or not
  the signing intent used is different than the one defined in container.yaml
- `build.extra.image.yum_repourls` (str list): Provided and inherited yum
  repourls
- `build.extra.submitter` (string): Username that submitted the build via
  content generator API
- `build.owner` (string or null): Username that started the task
- `build.extra.image.go` (map): Information about container first Go modules
- `build.extra.image.go.modules` (map list): Entries with Go modules information
- `build.extra.operator_manifests_archive` (string): Name of the archive
  containing operator manifest files. Included here for legacy reasons.
  `build.extra.typeinfo.operator-manifests.archive` Should be preferred
- `build.extra.image.pnc` (map): Information about middleware artifacts fetched
  using fetch-artifacts-pnc

The index map has these entries

- `pull` (str list): docker pull specifications for the manifest list, by tag
  and by digest
- `tags` (str list): Primary tags applied to the manifest list when it was
  created
- `floating_tags` (str list): Floating tags applied to the manifest list when it
  was created
- `unique_tags` (str list): Unique tags applied to the manifest list when it was
  created
- `digests` (map): A map of media type (such as
  "application/vnd.docker.distribution.manifest.list.v2+json") to manifest
  digest (a string, usually starting "sha256:"), for each grouped object; note
  that this will include manifest lists but not image manifests

The `build.extra.image.go.modules` entries are maps composed of the following
entries

- `module` (str): Module name for the top-level Go language package to be built,
  as in `example.com/go/packagename`
- `archive` (str): Possibly-compressed archive containing full source code
  including dependencies
- `path` (str): Path to directory containing source code (or its parent),
  possibly within archive

For operator builds, the operator manifests metadata is placed in
`build.extra.typeinfo.operator-manifests`. If this is present, the data in
`build.extra.image` will also be appended into `build.extra.typeinfo.image` by
koji.

Data which is placed here includes:

- `build.extra.typeinfo.operator-manifests.archive` (str): Name of the
  archive containing operator manifest files

General build info metadata is placed in `build.extra.osbs_build`.

Data which is placed here includes

- `build.extra.osbs_build.kind` (str): Build kind, either `container_build` or
  `source_container_build`
- `build.extra.osbs_build.subtypes`(str list): Build subtypes: `flatpak`,
  `operator_appregistry`, `operator_bundle`. Others might be added in future
- `build.extra.osbs_build.engine` (str): Build engine with which to build image,
  `bsi` (for source containers), `docker_api`, `imagebuilder`, `buildah_bud`

### Source container metadata

The same note about `image`, `build.extra.typeinfo.image` and
`build.extra.osbs_build` from previous section applies here.

Data which is placed here includes:

- `build.extra.image.media_types`: the same as for image builds (see previous
  section)
- `build.extra.image.sources_for_nvr` (str): koji N-V-R for the original image
  build
- `build.extra.image.sources_signing_intent` (str): signing intent used to fetch
  source RPMs
- `build.extra.submitter`: the same as for regular image builds (see previous
  section)

### Buildroot metadata

In each buildroot, the extra.osbs key is used to define a map that contains
these items:

- `build_id` (str): the build ID which resulted in the buildroot currently
  running atomic-reactor (**currently incorrect**)
- `builder_image_id` (str): the docker pull-by-digest specification for the
  buildroot currently running atomic-reactor
- `koji` (dict): a dictionary containing the following items
  - `build_name` (str): the koji name-nvr of the build
  - `builder_image_id` (dict): the docker digests of the build if availble or an
    empty dict otherwise.

### Output metadata

Each output has a type field. The docker image archive output is identified by
type "docker-image", and has these type-specific data:

- `extra.image.arch` (str): architecture for this image archive
- `extra.docker` (map): information specific to the Docker image

The docker map has these entries:

- `id` (str): the image ID ― for Docker 1.10 and higher this is a
  content-aware image ID
- `parent_id` (str): the image ID of the parent image
- `repositories` (str list): docker pull specifications for the image in the
  docker registry (or in Crane, if Pulp/Crane integration is used), by tag and
  by digests (there may be multiple digests, e.g. v2 schema 1 and v2 schema 2)
- `config` (map): The "v2 schema 2" ['config'][] object but with the
  `container_config` entry removed
- `tags` (str list): the image tags (i.e. the part after the ':' ) applied to
  this image when it was tagged and pushed
- `layer_sizes` (map list): the image layer uncompressed sizes, the oldest layer
  first (the size information comes from docker history command)
- `digests` (map): a map of media type (such as
  "application/vnd.docker.distribution.manifest.v2+json") to manifest digest (a
  string usually starting "sha256:"), for each available media type which can be
  retrieved by digest (media types only reachable by tag are not included).

### Type-specific metadata: Example

Content generator metadata in context:

```json
{
  "metadata_version": 0,
  "build": {
    "name": "package-name-docker",
    "version": "1.0.0",
    "release": "1",
    "owner": "kojiadmin",
    "extra": {
      "image": {
        "autorebuild": false,
        "isolated": false,
        "help": null,
        "parent_build_id": 123456
      },
      "submitter": "osbs",
      "filesystem_koji_task_id": 123457,
      "container_koji_task_id": 123456
    },
    "start_time": ...,
    "end_time": ...,
    "source": "git://...#..."
  },
  "buildroots": [
    {
      "id": 1,
      "container": {
        "type": "docker",
        "arch": "x86_64"
      },
      "extra": {
        "osbs": {
          "build_id": "(should be build which created the buildroot image)",
          "builder_image_id": "docker-pullable://.../buildroot@sha256:abcdef..."
          "koji": {
            "name": package-name:1.0.0-1",
            "builder_image_id": {
              "application/vnd.docker.distribution.manifest.v1+json": "sha256:123abc",
              "application/vnd.docker.distribution.manifest.v2+json": "sha256:123def"
            }
        }
      },
      "content_generator": {
        "name": "atomic-reactor",
        "version": "1.2.3"
      },
      "host": {...},
      "components": [...],
      "tools": [...]
    }
  ],
  "output": [
    {
      "type": "docker-image",
      "extra": {
        "image": {
          "arch": "x86_64"
        }
      },
      "docker": {
        "id": "sha256:abc123def...",
        "parent_id": "sha256:123def456...",
        "layer_sizes": [
            {"diff_id": "sha256:123def456...",
             "size": 1234556},
            {"diff_id": "sha256:456789013...",
             "size": 9494949}
        ],
        "repositories": [
          "registry.example.com/product/package-name:1.0.0-1",
          "registry.example.com/product/package-name@sha256:123abc...",
          "registry.example.com/product/package-name@sha256:123def..."
        ],
        "config": {
          "docker_version": "1.10.3",
          "rootfs": {...},
          "config": {...},
          ...
        },
        "tags": [
          "1.0.0-1"
        ],
        "digests": {
          "application/vnd.docker.distribution.manifest.v1+json": "sha256:123abc",
          "application/vnd.docker.distribution.manifest.v2+json": "sha256:123def"
        }
      },
      "components": [...],
      ...
    },
    ...
  ]
}
```

[Koji]: https://docs.pagure.org/koji
[described here]: ./base_images.md
[API]: https://docs.pagure.org/koji/content_generators
[specification]: https://docs.pagure.org/koji/content_generator_metadata
[format]: https://github.com/docker/docker/blob/master/image/spec/v1.2.md#combined-image-json--filesystem-changeset-format
["koji tags and targets"]: https://docs.pagure.org/koji/#tags-and-targets
[modules]: https://docs.pagure.org/modularity
['config']: https://docs.docker.com/registry/spec/manifest-v2-2/#image-manifest-field-descriptions
