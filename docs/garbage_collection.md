# Garbage collection when used with OpenShift

When client starts a build, the following happens:

1. OpenShift creates the build and pod objects. Build container with Atomic Reactor is
   spawned. Pod container is spawned to manage the pod with the build.
2. In the build container, Atomic Reactor determines the base image from the provided
   Dockerfile and pulls it (to the OpenShift node the pod runs on). Atomic Reactor also
   runs pre-build plugins but all of them currently only modify the build
   container filesystem and need no special garbage collection.
3. Docker build is started. If successful, the built image is added to the node's
   docker instance.
4. Pre-publish plugins are run.
   * `squash` creates new image by squashing the layers of built image. The
     original image is deleted.
5. Post-build plugins are run.
   * `all_rpm_packages` plugin creates container from the built image, runs it,
     and then deletes it.
6. Exit plugins are run.
   * `remove_built_image` removes the built image and the pulled base image
     from the set of node's docker images.

## Built images

The plugin `remove_built_image` deletes the base and built image. These images
may leak if the build fails in a way that prevents exit plugins from running.

## Base images

Base images are also removed by the `remove_built_image` plugin.

We may want to keep some of the base images in order to speed up future builds
that have the same base image ([Issue #146](https://github.com/projectatomic/atomic-reactor/issues/146)).

## Additional images

The `squash` pre-publish plugin creates new image but deletes the old one and
gives its tag to the new image. This means the garbage collection behaviour
should be the same whether the plugin is run or not, unless the plugin is run
with `dont_load=False` and `remove_former_image=False` in which case the
original image is not deleted after build.

## Build containers

OpenShift/Kubernetes [delete](https://github.com/openshift/origin/issues/1859)
finished build containers and pod containers when their number exceeds 100 -
the garbage collection mechanism tries to keep the finished containers below
this number.

## Additional containers

The only additional container is created by the `all_rpm_packages` plugin which
deletes it as well.

## OpenShift metadata

We don't delete the OpenShift/Kubernetes `Build` and `Pod` (FIXME: any other?)
objects created during the build.

We need to figure out if these objects take significant memory/disk space and
implement some garbage collection if they do.
