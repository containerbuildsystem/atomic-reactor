"""
Pre build plugin for koji build system
"""
import koji

from dock.plugin import PreBuildPlugin


class KojiPlugin(PreBuildPlugin):
    key = "koji"
    can_fail = False

    def __init__(self, tasker, workflow, target, hub, root):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param target: string, koji target to use as a source
        :param hub: string, koji hub (xmlrpc)
        :param root: string, koji root (storage)
        """
        # call parent constructor
        super(KojiPlugin, self).__init__(tasker, workflow)
        self.target = target
        self.xmlrpc = koji.ClientSession(hub)
        self.pathinfo = koji.PathInfo(topdir=root)

    def run(self):
        """
        run the plugin
        """
        target_info = self.xmlrpc.getBuildTarget(self.target)
        if target_info is None:
            self.log.error("provided target '%s' doesn't exist", self.target)
            raise RuntimeError("Provided target '%s' doesn't exist!" % self.target)
        tag_info = self.xmlrpc.getTag(target_info['build_tag_name'])
        repo_info = self.xmlrpc.getRepo(tag_info['id'])
        baseurl = self.pathinfo.repo(repo_info['id'], tag_info['name']) + r'/\$basearch'

        self.workflow.repos.setdefault('yum', [])
        repo = {
            'name': 'dock-koji-plugin-%s' % self.target,
            'baseurl': baseurl,
            'enabled': 1,
            'gpgcheck': 0,
        }
        self.workflow.repos['yum'].append(repo)
