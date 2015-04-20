from __future__ import print_function, unicode_literals
"""
Push built image to pulp registry
"""

from dock.plugin import PostBuildPlugin
from dock.util import ImageName

import json
import os

import requests


class PulpServer(object):
    """Interact with Pulp API"""
    def __init__(self, server_url, username, password, verify_ssl, tasker, logger):
        self._server_url = server_url
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._web_distributor = "docker_web_distributor_name_cli"
        self._export_distributor = "docker_export_distributor_name_cli"
        self._importer = "docker_importer"
        self._export_dir = "/var/www/pub/docker/web/"
        self._unit_type_id = "docker_image"
        self._chunk_size = 1048576  # 1 MB per upload call
        self.tasker = tasker
        self.logger = logger

    def _call_pulp(self, url, req_type='get', payload=None):
        if req_type == 'get':
            self.logger.info('Calling Pulp URL "{0}"'.format(url))
            r = requests.get(url, auth=(self._username, self._password), verify=self._verify_ssl)
        elif req_type == 'post':
            self.logger.info('Posting to Pulp URL "{0}"'.format(url))
            if payload:
                self.logger.debug('Pulp HTTP payload:\n{0}'.format(json.dumps(payload, indent=2)))
            r = requests.post(url, auth=(self._username, self._password), data=json.dumps(payload), verify=self._verify_ssl)
        elif req_type == 'put':
            # some calls pass in binary data so we don't log payload data or json encode it here
            self.logger.info('Putting to Pulp URL "{0}"'.format(url))
            r = requests.put(url, auth=(self._username, self._password), data=payload, verify=self._verify_ssl)
        elif req_type == 'delete':
            self.logger.info('Delete call to Pulp URL "{0}"'.format(url))
            r = requests.delete(url, auth=(self._username, self._password), verify=self._verify_ssl)
        else:
            raise ValueError('Invalid value of "req_type" parameter: {0}'.format(req_type))
        r_json = r.json()
        # some requests return null
        if not r_json:
            return r_json

        self.logger.debug('Pulp HTTP status code: {0}'.format(r.status_code))
        self.logger.debug('Pulp JSON response:\n{0}'.format(json.dumps(r_json, indent=2)))

        if 'error_message' in r_json:
            self.logger.warn('Error messages from Pulp response:\n{0}'.format(r_json['error_message']))

        if 'spawned_tasks' in r_json:
            for task in r_json['spawned_tasks']:
                self.logger.debug('Checking status of spawned task {0}'.format(task['task_id']))
                self._call_pulp('{0}/{1}'.format(self._server_url, task['_href']))
        return r_json

    @property
    def status(self):
        """Return pulp server status"""
        self.logger.info('Verifying Pulp server status')
        return self._call_pulp('{0}/pulp/api/v2/status/'.format(self._server_url))

    def verify_repo(self, repo_id):
        """Verify pulp repository exists"""
        url = '{0}/pulp/api/v2/repositories/{1}/'.format(self._server_url, repo_id)
        self.logger.info('Verifying pulp repository "{0}"'.format(repo_id))
        r_json = self._call_pulp(url)
        if 'error_message' in r_json:
            raise Exception('Repository "{0}" not found'.format(repo_id))

    def is_repo(self, repo_id):
        """Return true if repo exists"""
        url = '{0}/pulp/api/v2/repositories/'.format(self._server_url)
        self.logger.info('Verifying pulp repository "{0}"'.format(repo_id))
        r_json = self._call_pulp(url)
        return repo_id in [repo['id'] for repo in r_json]

    def create_repo(self, image, repo_id):
        """Create pulp docker repository"""
        payload = {
            'id': repo_id,
            'display_name': image,
            'description': 'docker image repository',
            'notes': {
                '_repo-type': 'docker-repo'
            },
            'importer_type_id': self._importer,
            'importer_config': {},
            'distributors': [{
                'distributor_type_id': 'docker_distributor_web',
                'distributor_id': self._web_distributor,
                'repo-registry-id': image,
                'auto_publish': 'true'},
                {
                'distributor_type_id': 'docker_distributor_export',
                'distributor_id': self._export_distributor,
                'repo-registry-id': image,
                'docker_publish_directory': self._export_dir,
                'auto_publish': 'true'}
                ]
        }
        url = '{0}/pulp/api/v2/repositories/'.format(self._server_url)
        self.logger.info('Verifying pulp repository "{0}"'.format(repo_id))
        r_json = self._call_pulp(url, "post", payload)
        if 'error_message' in r_json:
            raise Exception('Failed to create repository "{0}"'.format(repo_id))

    def update_redirect_url(self, repo_id, redirect_url):
        """Update distributor redirect URL and export file"""
        url = '{0}/pulp/api/v2/repositories/{1}/distributors/{2}/'.format(self._server_url, repo_id, self._export_distributor)
        payload = {
          "distributor_config": {
            "redirect-url": redirect_url
          }
        }
        self.logger.info('Update pulp repository "{0}" URL "{1}"'.format(repo_id, redirect_url))
        r_json = self._call_pulp(url, "put", json.dumps(payload))
        if 'error_message' in r_json:
            raise Exception('Unable to update pulp repo "{0}"'.format(repo_id))

    @property
    def _upload_id(self):
        """Get a pulp upload ID"""
        url = '{0}/pulp/api/v2/content/uploads/'.format(self._server_url)
        r_json = self._call_pulp(url, "post")
        if 'error_message' in r_json:
            raise Exception('Unable to get a pulp upload ID')
        return r_json['upload_id']

    def _delete_upload_id(self, upload_id):
        """Delete upload request ID"""
        self.logger.info('Deleting pulp upload ID {0}'.format(upload_id))
        url = '{0}/pulp/api/v2/content/uploads/{1}/'.format(self._server_url, upload_id)
        self._call_pulp(url, "delete")

    def upload_image_from_tarfile(self, repo_id, file_upload):
        """Upload image to pulp repository"""
        if not os.path.isfile(file_upload):
            raise Exception('Cannot find file "{0}"'.format(file_upload))
        else:
            upload_id = self._upload_id
            self.logger.info('Uploading image using ID "{0}"'.format(upload_id))
            self._upload_bits(upload_id, file_upload)
            self._import_upload(upload_id, repo_id)
            self._delete_upload_id(upload_id)

    def upload_docker_image(self, image, repo_id):
        """Upload image to pulp repository"""
        if not self.tasker.inspect_image(ImageName.parse(image)):
            raise Exception("Image doesn't exist '{0}'".format(image))
        else:
            upload_id = self._upload_id
            self.logger.info('Uploading image using ID "{0}"'.format(upload_id))
            self._upload_docker_image(upload_id, image)
            self._import_upload(upload_id, repo_id)
            self._delete_upload_id(upload_id)

    def _upload_docker_image(self, upload_id, image):
        self.logger.info('Uploading docker image ({0})'.format(image))
        offset = 0
        image_stream = self.tasker.d.get_image(image)
        while True:
            # image_stream.seek(offset)
            data = image_stream.read(self._chunk_size)
            if not data:
                break
            url = '{0}/pulp/api/v2/content/uploads/{1}/{2}/'.format(self._server_url, upload_id, offset)
            self.logger.info('Uploading {0}: {1}'.format(image, offset))
            self._call_pulp(url, "put", data)
            offset += self._chunk_size
        image_stream.close()

    def _upload_bits(self, upload_id, file_upload):
        self.logger.info('Uploading file ({0})'.format(file_upload))
        offset = 0
        source_file_size = os.path.getsize(file_upload)
        f = open(file_upload, 'r')
        while True:
            f.seek(offset)
            data = f.read(self._chunk_size)
            if not data:
                break
            url = '{0}/pulp/api/v2/content/uploads/{1}/{2}/'.format(self._server_url, upload_id, offset)
            self.logger.info('Uploading {0}: {1} of {2} bytes'.format(file_upload, offset, source_file_size))
            self._call_pulp(url, "put", data)
            offset = min(offset + self._chunk_size, source_file_size)
        f.close()

    def _import_upload(self, upload_id, repo_id):
        """Import uploaded content"""
        self.logger.info('Importing pulp upload {0} into {1}'.format(upload_id, repo_id))
        url = '{0}/pulp/api/v2/repositories/{1}/actions/import_upload/'.format(self._server_url, repo_id)
        payload = {
          'upload_id': upload_id,
          'unit_type_id': self._unit_type_id,
          'unit_key': None,
          'unit_metadata': None,
          'override_config': None
        }
        r_json = self._call_pulp(url, "post", payload)
        if 'error_message' in r_json:
            raise Exception('Unable to import pulp content into {0}'.format(repo_id))

    def _publish_repo(self, repo_id):
        """Publish pulp repository to pulp web server"""
        url = '{0}/pulp/api/v2/repositories/{1}/actions/publish/'.format(self._server_url, repo_id)
        payload = {
          "id": self._web_distributor,
          "override_config": {}
        }
        self.logger.info('Publishing pulp repository "{0}"'.format(repo_id))
        r_json = self._call_pulp(url, "post", payload)
        if 'error_message' in r_json:
            raise Exception('Unable to publish pulp repo "{0}"'.format(repo_id))

    def export_repo(self, repo_id):
        """Export pulp repository to pulp web server as tar

        The tarball is split into the layer components and crane metadata.
        It is for the purpose of uploading to remote crane server"""
        url = '{0}/pulp/api/v2/repositories/{1}/actions/publish/'.format(self._server_url, repo_id)
        payload = {
          "id": self._export_distributor,
          "override_config": {
            "export_file": '{0}{1}.tar'.format(self._export_dir, repo_id),
          }
        }
        self.logger.info('Exporting pulp repository "{0}"'.format(repo_id))
        r_json = self._call_pulp(url, "post", payload)
        if 'error_message' in r_json:
            raise Exception('Unable to export pulp repo "{0}"'.format(repo_id))


def push_image_to_pulp(repo, image, server_url, username, password, verify_ssl, tasker, logger):
    try:
        pulp = PulpServer(server_url=server_url, username=username,
                          password=password, verify_ssl=verify_ssl, tasker=tasker, logger=logger)
        logger.info("pulp server status: %s", pulp.status)
    except Exception as e:
        logger.critical('Failed to initialize Pulp: {0}'.format(e))
        return
    else:
        if not pulp.is_repo(repo):
            try:
                pulp.create_repo(image, repo)
            except Exception as e:
                logger.critical('Failed to create Pulp repository: {0}'.format(e))
        try:
            pulp.upload_docker_image(image, repo)
            logger.info('Uploaded image to pulp repo "{0}"'.format("busybox"))
        except Exception as e:
            logger.error('Failed to upload image to Pulp: {0}'.format(e))
            raise
        else:
            pulp.export_repo(repo)


class PulpPushPlugin(PostBuildPlugin):
    key = "pulp_push"

    def __init__(self, tasker, workflow, image, server_url=None, username=None, password=None, verify_ssl=True):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param image: str, docker image to push to pulp registry
        :param server_url: str, URL to pulp server
        :param username: str
        :param password: str
        :param verify_ssl: str, verify certificate of the SSL connection
        """
        # call parent constructor
        super(PulpPushPlugin, self).__init__(tasker, workflow)
        self.image = image
        self.server_url = server_url or self.get_or_raise(os.environ, 'PULP_SERVER_URL')
        self.username = username or self.get_or_raise(os.environ, 'PULP_USERNAME')
        self.password = password or self.get_or_raise(os.environ, 'PULP_PASSWORD')
        self.verify_ssl = verify_ssl

    def get_or_raise(self, d, k):
        try:
            return d[k]
        except KeyError:
            self.log.error("there is no key '%s'", k)
            raise RuntimeError("missing key '%s'")

    def run(self):
        repo = self.image.split(":")[0].replace("/", '-')
        return push_image_to_pulp(
            repo,
            self.image,
            self.server_url,
            self.username,
            self.password,
            self.verify_ssl,
            self.tasker,
            self.log,
        )
