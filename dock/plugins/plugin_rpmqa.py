from dock.plugin import PostBuildPlugin


__all__ = ('PostBuildRPMqaPlugin', )


class PostBuildRPMqaPlugin(PostBuildPlugin):
    def __init__(self):
        """ """

    @property
    def key(self):
        """ result of plugin will be under this key in response dict """
        return "all_packages"

    @property
    def command(self):
        """ command to run in image """
        return "/bin/rpm -qa"

    def __str__(self):
        return "%s: `%s`" % (self.key, self.command)
