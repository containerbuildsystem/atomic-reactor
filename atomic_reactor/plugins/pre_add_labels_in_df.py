"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pre build plugin which adds labels to dockerfile. Labels have to be specified either
as a dict:

{
    "name": "add_labels_in_dockerfile",
    "args": {
        "labels": {
            "label1": "value1",
            "label 2": "some value"
        }
    }
}

Or as a string, which must be a dict serialised as JSON.

this will add turn this dockerfile:

```dockerfile
FROM fedora
CMD date
```

into this:

```dockerfile
FROM fedora
LABEL "label1"="value1" "label 2"="some value"
CMD date
```

Keys and values are quoted as necessary.
"""

from __future__ import unicode_literals

from dockerfile_parse import DockerfileParser
from atomic_reactor.plugin import PreBuildPlugin
import json
import datetime


class AddLabelsPlugin(PreBuildPlugin):
    key = "add_labels_in_dockerfile"

    def __init__(self, tasker, workflow, labels, dont_overwrite=("Architecture", ),
                 auto_labels=("build-date", "architecture", "vcs-type", "vcs-ref")):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param labels: dict, key value pairs to set as labels; or str, JSON-encoded dict
        :param dont_overwrite: iterable, list of label keys which should not be overwritten
        :param auto_labels: iterable, list of labels to be determined automatically, if supported
        """
        # call parent constructor
        super(AddLabelsPlugin, self).__init__(tasker, workflow)
        if isinstance(labels, str):
            labels = json.loads(labels)
        if not isinstance(labels, dict):
            raise RuntimeError("labels have to be dict")
        self.labels = labels
        self.dont_overwrite = dont_overwrite

        self.generate_auto_labels(auto_labels)

    def generate_auto_labels(self, auto_labels):
        generated = {}

        # build date
        rfc3339_ts = datetime.datetime.utcnow().isoformat()
        rfc3339_ts += 'Z'
        generated['build-date'] = rfc3339_ts

        # architecture - assuming host and image architecture is the same
        # TODO: this code is also in plugins/exit_koji_promote.py, factor it out
        docker_version = self.tasker.get_version()
        host_arch = docker_version['Arch']
        if host_arch == 'amd64':
            host_arch = 'x86_64'
        generated['architecture'] = host_arch

        # VCS info
        vcs = self.workflow.source.get_vcs_info()
        if vcs:
            generated['vcs-type'] = vcs.vcs_type
            generated['vcs-url'] = vcs.vcs_url
            generated['vcs-ref'] = vcs.vcs_ref

        for lbl in auto_labels:
            if lbl in self.labels:
                self.log.info("label %s is set explicitly, not using generated value", lbl)
                continue

            if lbl in generated:
                self.labels[lbl] = generated[lbl]
            else:
                self.log.warning("requested automatic label %s is not available", lbl)

    def run(self):
        """
        run the plugin
        """
        dockerfile = DockerfileParser(self.workflow.builder.df_path)
        lines = dockerfile.lines

        # correct syntax is:
        #   LABEL "key"="value" "key2"="value2"

        # Make sure to escape '\' and '"' characters.
        try:
            # py3
            env_trans = str.maketrans({'\\': '\\\\',
                                       '"': '\\"'})
        except AttributeError:
            # py2
            env_trans = None

        def escape(s):
            if env_trans:
                return s.translate(env_trans)
            return s.replace('\\', '\\\\').replace('"', '\\"')

        labels = []
        for key, value in self.labels.items():
            try:
                base_image_value = self.workflow.base_image_inspect["Config"]["Labels"][key]
            except KeyError:
                self.log.info("label %r not present in base image", key)
            except (AttributeError, TypeError):
                self.log.warning("base image was not inspected")
                break
            else:
                if base_image_value == value:
                    self.log.info("label %r is already set to %r", key, value)
                    continue
                else:
                    self.log.info("base image has label %r set to %r", key, base_image_value)
                    if key in self.dont_overwrite:
                        self.log.info("denying overwrite of label %r", key)
                        continue

            label = '"%s"="%s"' % (escape(key), escape(value))
            self.log.info("setting label %s", label)
            labels.append(label)

        content = ""
        if labels:
            content = 'LABEL ' + " ".join(labels)
            # put labels at the end of dockerfile (since they change metadata and do not interact
            # with FS, this should cause no harm)
            lines.append('\n' + content + '\n')
            dockerfile.lines = lines

        return content
