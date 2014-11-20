from dock.plugin import PostBuildPlugin


__all__ = ('PostBuildRPMqaPlugin', )


class PostBuildRPMqaPlugin(PostBuildPlugin):
    def __init__(self):
        """ """

    @property
    def key(self):
        """ result of plugin will be under this key in response dict """
        return "all_rpm_packages"

    @property
    def command(self):
        """ command to run in image """
        return "-qa"

    @property
    def create_container_kwargs(self):
        """ keyword arguments for create_container """
        return {"entrypoint": "/bin/rpm"}

    @property
    def start_container_kwargs(self):
        """ keyword arguments for start_container """
        return {}

    def __str__(self):
        return "%s: `%s`" % (self.key, self.command)
