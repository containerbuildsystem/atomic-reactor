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


class AddLabelsPlugin(PreBuildPlugin):
    key = "add_labels_in_dockerfile"

    def __init__(self, tasker, workflow, labels, dont_overwrite=("Architecture", )):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param labels: dict, key value pairs to set as labels; or str, JSON-encoded dict
        :param dont_overwrite: iterable, list of label keys which should not be overwritten
        """
        # call parent constructor
        super(AddLabelsPlugin, self).__init__(tasker, workflow)
        if isinstance(labels, str):
            labels = json.loads(labels)
        if not isinstance(labels, dict):
            raise RuntimeError("labels have to be dict")
        self.labels = labels
        self.dont_overwrite = dont_overwrite

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
                self.log.info("label %s not present in base image", repr(key))
            except (AttributeError, TypeError):
                self.log.warning("base image was not inspected")
                break
            else:
                if base_image_value == value:
                    self.log.info("label %s is already set to %s", repr(key), repr(value))
                    continue
                else:
                    self.log.info("base image has label %s set to %s", repr(key), repr(base_image_value))
                    if key in self.dont_overwrite:
                        self.log.info("denying overwrite of label %s", repr(key))
                        continue

            label = '"%s"="%s"' % (escape(key), escape(value))
            self.log.info("setting label %s", label)
            labels.append(label)

        content = ""
        if labels:
            content = 'LABEL ' + " ".join(labels)
            num_lines = len(lines)
            if num_lines < 2:
                # ensure label is after FROM if its the only instruction in the Docker
                lines.append('\n' + content + '\n')
            else:
                # put it before last instruction
                lines.insert(-1, content + '\n')

            dockerfile.lines = lines

        return content
