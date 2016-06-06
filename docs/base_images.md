# Building Base Images

A base image is simply an image that has no parent. Atomic reactor can be
used to build base images by using Koji's image build functionality via
the `add_filesystem` plugin.

## Add Filesystem Plugin

1. Inspects the `FROM` instruction in Dockerfile. If value is "koji/image-build",
  this plugin proceeds. Otherwise, no further processing is performed and the
  plugin quits.

2. Parses the image build configuration from `image-build.conf` which is used
  to create an image build task in Koji. A different name for the image build
  configuration file can be specified by using a tag in the `FROM` instruction.
  For example, `FROM koji/image-build:custom.conf` will load configuration
  from `custom.conf` file.`

3. Downloads produced filesystem tarball from Koji and uses docker's `import`
  to load it as an image. The `FROM` instruction in Dockerfile is modified
  to use the ID of the newly imported image.

4. Build proceeds as usual.

Use the `from_task_id` plugin parameter to use the tarball from an existing
Koji task.

## Image Build Configuration

The image build configuration file is used to specify how the filesystem will
be created.
[More Info](https://fedoraproject.org/wiki/Koji/BuildingImages#Building_Disk_Images).
The syntax for this file is equivalent to the syntax used when in the config
file used for Koji's CLI client image build subcommand:
`koji image-build --config config`

For convenience, defaults are provided to minimize the size of the configuration
file. See the `DEFAULT_IMAGE_BUILD_CONF` variable in `add_filesystem` plugin's
[source](/atomic_reactor/plugins/pre_add_filesystem.py) for all defaults.
Most notably, the `ksurl` item is by default dynamically mapped to the current
branch and commit.

It's important to note that currently in the `[factory-parameters]` section,
the `create_docker_metadata` parameter must be set to `False`. Setting this
parameter to `True`, will alter the format of the tarball produced by Koji which
the `add_filesystem` plugin does not support.
