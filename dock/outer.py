"""

"""
import json
import os
import shutil
import tempfile

from dock import CONTAINER_SHARE_PATH, BUILD_JSON
from dock.core import DockerTasker


class PrivilegedDockerBuilder(object):
    def __init__(self, build_image_id, build_args):
        self.build_image_id = build_image_id
        self.build_args = build_args

    def build(self):
        """
        build image from provided build_args

        :return:
        """
        temp_dir = tempfile.mkdtemp()
        try:
            with open(os.path.join(temp_dir, BUILD_JSON), 'w') as build_json:
                json.dump(self.build_args, build_json)
            dt = DockerTasker()
            container_id = dt.run(
                self.build_image_id,
                create_kwargs={'volumes': [temp_dir]},
                start_kwargs={'binds': {temp_dir: {'bind': CONTAINER_SHARE_PATH, 'rw': True}},
                              'privileged': True}
            )
            dt.wait(container_id)
            # TODO send info back
        finally:
            shutil.rmtree(temp_dir)