Configuration file
==================

Atomic-reactor has a plugin, reactor_config, which can accept a pathname to a YAML file describing the configuration for atomic-reactor.

It has the following keys:

**version** which must be 1.

**clusters** is a map of platform names, with each value being a list. Each list item describes an OpenShift cluster that can handle builds for that platform.

The cluster description includes a **name**, which must correspond to the instance names in the osbs.conf available to atomic-reactor; a **max_concurrent_builds** integer describing how many worker builds this cluster should be allowed to handle; and an optional **enabled** boolean which defaults to true.

Example:

```yaml
version: 1
clusters:
  x86_64:
  - name: worker01
    max_concurrent_builds: 4
    enabled: true
  - name: worker02
    max_concurrent_builds: 8
    enabled: false
  - name: worker03
    max_concurrent_builds: 8
```

In this example builds for the x86_64 platform can be sent to worker01 if it has fewer than 4 active worker builds, or worker03.

The full schema is available in [config.json](https://github.com/projectatomic/atomic-reactor/blob/master/atomic_reactor/schemas/config.json).
