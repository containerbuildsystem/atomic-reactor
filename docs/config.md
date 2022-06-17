# Configuration File

Atomic-reactor has a plugin, `reactor_config`, which can accept a pathname to a
YAML file describing atomic-reactor's configuration.

It has the following keys

- **version**: Must be 1.
- **registries_cfg_path**: A directory path where holds a docker
  configuration file for registry authentication. Either `.dockercfg`
  or `.dockerconfigjson` is supported.
- **remote_hosts**: Contains 'pools' which is a map of platform names,
  with each value being a list. Each list item describes host which can handle
  builds for that platform.
  'slots_dir' specifies directory for storing information about slots.

The host description includes

- **hostname**: host name of the host
- **auth**: path to authentication for a host
- **enabled**: Optional; boolean which defaults to true.
- **slots**: An integer specifying how many builds this host
  should be allowed to handle
- **socket_path**: path to podman socket
- **username**: user used for building

Example:

```yaml
version: 1
remote_hosts:
    pools:
        x86_64:
            x86-64-hostname1:
                auth: /path/to/host/authentication/remote-host-auth
                enabled: true
                slots: 10
                socket_path: /path/to/podman/socket/podman.sock
                username: building_user
            x86-64-hostname2:
                auth: /path/to/host/authentication/remote-host-auth
                enabled: true
                slots: 5
                socket_path: /path/to/podman/socket/podman.sock
                username: building_user
    slots_dir: /path/to/slots/directory
```

In this example, builds for the `x86_64` platform can be sent
to `x86-64-hostname1` if it has fewer than 10 active builds,
or to `x86-64-hostname2` if it has fewer than 5 active builds.

The full schema is available in [config.json][].

[config.json]: ../atomic_reactor/schemas/config.json
