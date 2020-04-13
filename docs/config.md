# Configuration File

Atomic-reactor has a plugin, `reactor_config`, which can accept a pathname to a
YAML file describing atomic-reactor's configuration.

It has the following keys

- **version**: Must be 1.
- **clusters**: A map of platform names, with each value being a list. Each list
  item describes an OpenShift cluster that can handle builds for that platform.
  The list is in order of preference, most preferred first.

The cluster description includes

- **name**: Must correspond to the instance names in the osbs.conf available to
  atomic-reactor
- **max_concurrent_builds**: An integer specifying how many worker builds this
  cluster should be allowed to handle
- **enabled**: Optional; boolean which defaults to true.

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

In this example, builds for the `x86_64` platform can be sent to `worker01` if
it has fewer than 4 active worker builds, or `worker03`.

The full schema is available in [config.json][].

[config.json]: ../atomic_reactor/schemas/config.json
