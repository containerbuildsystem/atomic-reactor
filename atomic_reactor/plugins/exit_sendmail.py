"""
Copyright (c) 2015, 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import smtplib
import socket
import json
try:
    from urlparse import urljoin
except ImportError:
    from urllib.parse import urljoin

from atomic_reactor.plugin import ExitPlugin, PluginFailedException
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.plugins.exit_koji_import import KojiImportPlugin
from atomic_reactor.plugins.exit_koji_promote import KojiPromotePlugin
from atomic_reactor.plugins.exit_store_metadata_in_osv3 import StoreMetadataInOSv3Plugin
from atomic_reactor.plugins.pre_reactor_config import (get_smtp_session, get_koji_session,
                                                       get_smtp, get_koji, get_openshift,
                                                       get_openshift_session, get_koji_path_info)
from atomic_reactor.koji_util import get_koji_task_owner
from atomic_reactor.util import get_build_json, OSBSLogs, ImageName, df_parser
from atomic_reactor.constants import PLUGIN_SENDMAIL_KEY
from osbs.utils import Labels


# an email address consisting of local name, an @ sign, and a domain name
# the local name starts with a letter and then has one or more alphanumerics, _, +, and -
# symbols in any order.
# the domain name has one or more alphanumerics or - in any order
# followed by a ., followed by one or more alphanumerics and - in any order.
# RFC 2821 defines a valid email address much more expansively, but this definition matches
# what most people loosely expect to be valid.
# specifically, email addresses of the form id/name@domain.tla are not valid.
VALID_EMAIL_REGEX = r'^[a-zA-Z][a-zA-Z0-9-_.+]+@[a-zA-Z0-9-.]+\.[a-zA-Z0-9-]+$'


def validate_address(address):
    if address and re.match(VALID_EMAIL_REGEX, address) is not None:
        return True


class SendMailPlugin(ExitPlugin):
    """This plugins sends notifications about build results.

    Example configuration (see arguments for init for detailed explanation):
        "exit_plugins": [{
                "name": "sendmail",
                "args": {
                    "send_on": ["auto_canceled", "auto_fail"],
                    "url": "https://openshift-instance.com",
                    "smtp_host": "smtp-server.com",
                    "from_address": "osbs@mycompany.com",
                    "error_addresses": ["admin@mycompany.com", "manager@mycompany.com"],
                    "additional_addresses": ["jsmith@mycompany.com", "user@mycompany.com"],
                    "email_domain": "example.com",
                    "to_koji_submitter": True,
                    "to_koji_pkgowner": True,
                }
        }]
    """
    key = PLUGIN_SENDMAIL_KEY

    # symbolic constants for states
    MANUAL_SUCCESS = 'manual_success'
    MANUAL_FAIL = 'manual_fail'
    MANUAL_CANCELED = 'manual_canceled'
    AUTO_SUCCESS = 'auto_success'
    AUTO_FAIL = 'auto_fail'
    AUTO_CANCELED = 'auto_canceled'
    DEFAULT_SUBMITTER = 'Unknown'

    allowed_states = set([MANUAL_SUCCESS, MANUAL_FAIL, MANUAL_CANCELED,
                          AUTO_SUCCESS, AUTO_FAIL, AUTO_CANCELED])

    def __init__(self, tasker, workflow,
                 smtp_host=None, from_address=None,
                 send_on=(AUTO_CANCELED, AUTO_FAIL, MANUAL_SUCCESS, MANUAL_FAIL),
                 url=None,
                 error_addresses=(),
                 additional_addresses=(),
                 email_domain=None,
                 koji_hub=None,
                 koji_root=None,
                 koji_proxyuser=None,
                 koji_ssl_certs_dir=None,
                 koji_krb_principal=None,
                 koji_krb_keytab=None,
                 to_koji_submitter=False,
                 to_koji_pkgowner=False,
                 use_auth=None,
                 verify_ssl=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param send_on: list of str, list of build states when a notification should be sent
            see 'allowed_states' constant and rules in '_should_send' function
        :param url: str, URL to OSv3 instance where the build logs are stored
        :param smtp_host: str, URL of SMTP server to use to send the message (e.g. "foo.com:25")
        :param from_address: str, the "From" of the notification email
        :param error_addresses: list of str, list of email addresses where to send an email
            if an error occurred (e.g. if we can't find out who to notify about the failed build)
        :param additional_addresses: list of str, always send a message to these email addresses
        :param email_domain: str, email domain used when email addresses cannot be fetched via
            kerberos principal
        :param koji_hub: str, koji hub (xmlrpc)
        :param koji_root: str, koji root (storage)
        :param koji_proxyuser: str, proxy user
        :param koji_ssl_certs_dir: str, path to "cert", "ca", and "serverca"
        :param koji_krb_principal: str, name of Kerberos principal
        :param koji_krb_keytab: str, Kerberos keytab
        :param to_koji_submitter: bool, send a message to the koji submitter
        :param to_koji_pkgowner: bool, send messages to koji package owners
        """
        super(SendMailPlugin, self).__init__(tasker, workflow)
        self.submitter = self.DEFAULT_SUBMITTER
        self.send_on = set(send_on)

        self.smtp_fallback = {
            'host': smtp_host,
            'from_address': from_address,
            'additional_addresses': list(additional_addresses),
            'error_addresses': list(error_addresses),
            'domain': email_domain,
            'send_to_submitter': to_koji_submitter,
            'send_to_pkg_owner': to_koji_pkgowner
        }
        smtp = get_smtp(self.workflow, self.smtp_fallback)
        self.additional_addresses = smtp.get('additional_addresses', ())
        self.from_address = smtp['from_address']
        self.error_addresses = smtp.get('error_addresses', ())
        self.email_domain = smtp.get('domain')
        self.to_koji_submitter = smtp.get('send_to_submitter', False)
        self.to_koji_pkgowner = smtp.get('send_to_pkg_owner', False)

        self.koji_fallback = {
            'hub_url': koji_hub,
            'root_url': koji_root,
            'auth': {
                'proxyuser': koji_proxyuser,
                'ssl_certs_dir': koji_ssl_certs_dir,
                'krb_principal': str(koji_krb_principal),
                'krb_keytab_path': str(koji_krb_keytab)
            }
        }

        self.openshift_fallback = {
            'url': url,
            'insecure': not verify_ssl,
            'auth': {'enable': use_auth}
        }
        self.url = get_openshift(self.workflow, self.openshift_fallback)['url']

        try:
            metadata = get_build_json().get("metadata", {})
            self.koji_task_id = int(metadata['labels']['koji-task-id'])
        except Exception:
            self.log.exception("Failed to fetch koji task ID")
            self.koji_task_id = None
        else:
            self.log.info("Koji task ID: %s", self.koji_task_id)

        self.koji_build_id = self.workflow.exit_results.get(KojiImportPlugin.key)
        if not self.koji_build_id:
            self.koji_build_id = self.workflow.exit_results.get(KojiPromotePlugin.key)
            if not self.koji_build_id:
                self.log.info("Failed to fetch koji build ID")
            else:
                self.log.info("Koji build ID: %s", self.koji_build_id)
        else:
            self.log.info("Koji build ID: %s", self.koji_build_id)

        self.session = None
        if get_koji(self.workflow, self.koji_fallback)['hub_url']:
            try:
                self.session = get_koji_session(self.workflow, self.koji_fallback)
            except Exception:
                self.log.exception("Failed to connect to koji")
                self.session = None
            else:
                self.log.info("Koji connection established")

    def _fetch_log_files(self):
        osbs = get_openshift_session(self.workflow, self.openshift_fallback)
        build_id = get_build_json()['metadata']['name'] or {}
        osbs_logs = OSBSLogs(self.log)

        return osbs_logs.get_log_files(osbs, build_id)

    def _should_send(self, rebuild, success, auto_canceled, manual_canceled):
        """Return True if any state in `self.send_on` meets given conditions, thus meaning
        that a notification mail should be sent.
        """
        should_send = False

        should_send_mapping = {
            self.MANUAL_SUCCESS: not rebuild and success,
            self.MANUAL_FAIL: not rebuild and not success,
            self.MANUAL_CANCELED: not rebuild and manual_canceled,
            self.AUTO_SUCCESS: rebuild and success,
            self.AUTO_FAIL: rebuild and not success,
            self.AUTO_CANCELED: rebuild and auto_canceled
        }

        for state in self.send_on:
            should_send |= should_send_mapping[state]
        return should_send

    def _get_image_name_and_repos(self):

        repos = []
        dockerfile = df_parser(self.workflow.builder.df_path, workflow=self.workflow)
        labels = Labels(dockerfile.labels)
        _, image_name = labels.get_name_and_value(Labels.LABEL_TYPE_NAME)

        stored_data = self.workflow.exit_results.get(StoreMetadataInOSv3Plugin.key)
        if not stored_data or 'annotations' not in stored_data:
            raise ValueError('Stored Metadata not found')

        repo_data = json.loads(stored_data['annotations']['repositories'])

        repos.extend(repo_data.get('unique', []))
        repos.extend(repo_data.get('primary', []))
        repos.extend(repo_data.get('floating', []))

        if repos:
            image_name_obj = ImageName.parse(repos[0])
            image_name = image_name_obj.get_repo()

        return (image_name, repos)

    def _render_mail(self, rebuild, success, auto_canceled, manual_canceled):
        """Render and return subject and body of the mail to send."""
        subject_template = '%(endstate)s building image %(image_name)s'
        body_template = '\n'.join([
            'Image Name: %(image_name)s',
            'Repositories: %(repositories)s',
            'Status: %(endstate)s',
            'Submitted by: %(user)s',
        ])

        # Failed autorebuilds include logs as attachments.
        # Koji integration stores logs in successful Koji Builds.
        # Don't include logs in these cases.
        if self.session and not rebuild:
            body_template += '\nLogs: %(logs)s'

        endstate = None
        if auto_canceled or manual_canceled:
            endstate = 'Canceled'
        else:
            endstate = 'Succeeded' if success else 'Failed'

        url = self._get_logs_url()

        image_name, repos = self._get_image_name_and_repos()

        repositories = ''
        for repo in repos:
            repositories += '\n    ' + repo

        formatting_dict = {
            'repositories': repositories,
            'image_name': image_name,
            'endstate': endstate,
            'user': '<autorebuild>' if rebuild else self.submitter,
            'logs': url
        }

        vcs = self.workflow.source.get_vcs_info()
        if vcs:
            body_template = '\n'.join([
                body_template,
                'Source url: %(vcs-url)s',
                'Source ref: %(vcs-ref)s',
            ])
            formatting_dict['vcs-url'] = vcs.vcs_url
            formatting_dict['vcs-ref'] = vcs.vcs_ref

        log_files = None
        if rebuild and endstate == 'Failed':
            log_files = self._fetch_log_files()

        return (subject_template % formatting_dict, body_template % formatting_dict, log_files)

    def _send_mail(self, receivers_list, subject, body, log_files=None):
        if not receivers_list:
            self.log.info('no valid addresses in requested addresses. Doing nothing')
            return

        self.log.info('sending notification to %s ...', receivers_list)

        """Actually sends the mail with `subject` and `body` and optionanl log_file attachements
        to all members of `receivers_list`."""
        if log_files:
            msg = MIMEMultipart()
            msg.attach(MIMEText(body))
            for entry in log_files:
                log_mime = MIMEBase('application', "octet-stream")
                log_file = entry[0]  # Output.file
                log_file.seek(0)
                log_mime.set_payload(log_file.read())
                encoders.encode_base64(log_mime)
                log_mime.add_header('Content-Disposition',
                                    'attachment; filename="{}"'.format(entry[1]['filename']))
                msg.attach(log_mime)
        else:
            msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = self.from_address
        msg['To'] = ', '.join([x.strip() for x in receivers_list])

        s = None
        try:
            s = get_smtp_session(self.workflow, self.smtp_fallback)
            s.sendmail(self.from_address, receivers_list, msg.as_string())
        except (socket.gaierror, smtplib.SMTPException):
            self.log.error('Error communicating with SMTP server')
            raise
        finally:
            if s is not None:
                s.quit()

    def _get_email_from_koji_obj(self, obj):
        if obj.get('krb_principal'):
            return obj['krb_principal'].lower()
        else:
            if not self.email_domain:
                raise RuntimeError("Empty email_domain specified")
            return '@'.join([obj['name'], self.email_domain])

    def _get_koji_submitter(self):
        koji_task_owner = get_koji_task_owner(self.session, self.koji_task_id)
        koji_task_owner_email = self._get_email_from_koji_obj(koji_task_owner)
        self.submitter = koji_task_owner_email
        return koji_task_owner_email

    def _get_koji_owners(self):
        result = []
        if not self.koji_build_id:
            return result

        koji_build_info = self.session.getBuild(self.koji_build_id)

        koji_tags = self.session.listTags(self.koji_build_id)
        for koji_tag in koji_tags:
            koji_tag_id = koji_tag['id']
            koji_package_id = koji_build_info['package_id']
            koji_pkg_tag_config = self.session.getPackageConfig(koji_tag_id, koji_package_id)
            koji_pkg_tag_owner = self.session.getUser(koji_pkg_tag_config['owner_id'])

            result.append(self._get_email_from_koji_obj(koji_pkg_tag_owner))

        return result

    def _get_logs_url(self):
        url = None
        try:
            pathinfo = get_koji_path_info(self.workflow, self.koji_fallback)
            url = '/'.join([pathinfo.work(), pathinfo.taskrelpath(self.koji_task_id)])
        except Exception:
            self.log.exception("Failed to fetch logs from koji")
            if self.url and self.workflow.openshift_build_selflink:
                url = urljoin(self.url, self.workflow.openshift_build_selflink + '/log')
        return url

    def _get_receivers_list(self):
        receivers_list = []
        if self.additional_addresses:
            receivers_list += self.additional_addresses

        if self.session and (self.to_koji_submitter or self.to_koji_pkgowner):
            if self.to_koji_submitter:
                try:
                    koji_task_owner_email = self._get_koji_submitter()
                except Exception:
                    self.log.exception("Failed to include a task submitter")
                else:
                    receivers_list.append(koji_task_owner_email)

            if self.to_koji_pkgowner:
                try:
                    koji_task_owner_emails = self._get_koji_owners()
                except Exception:
                    self.log.exception("Failed to include a package owner")
                else:
                    receivers_list += koji_task_owner_emails

        # Remove duplicates
        receivers_list = list(set(receivers_list))

        # an empty list because of invalid addresses is not an error, so check for an
        # empty list before invalidating addresses
        if not receivers_list:
            raise RuntimeError("No recipients found")

        # Remove invalid items
        receivers_list = [x for x in receivers_list if validate_address(x)]

        return receivers_list

    def run(self):
        # verify that given states are subset of allowed states
        unknown_states = self.send_on - self.allowed_states
        if len(unknown_states) > 0:
            raise PluginFailedException('Unknown state(s) "%s" for sendmail plugin' %
                                        '", "'.join(sorted(unknown_states)))

        rebuild = is_rebuild(self.workflow)
        success = not self.workflow.build_process_failed
        auto_canceled = self.workflow.autorebuild_canceled
        manual_canceled = self.workflow.build_canceled

        self.log.info('checking conditions for sending notification ...')
        if self._should_send(rebuild, success, auto_canceled, manual_canceled):
            self.log.info('notification about build result will be sent')
            try:
                self.log.debug('getting list of receivers for this component ...')
                receivers = self._get_receivers_list()
            except RuntimeError as e:
                self.log.error('couldn\'t get list of receivers, sending error message ...')
                # Render the body although the receivers cannot be fetched for error message
                _, expected_body, _ = self._render_mail(
                    rebuild, success, auto_canceled, manual_canceled)
                body = '\n'.join([
                    'Failed to get contact for %s, error: %s' % (str(self.workflow.image), str(e)),
                    'Since your address is in "error_addresses", this email was sent to you to '
                    'take action on this.',
                    'Wanted to send following mail:',
                    '',
                    expected_body
                ])
                receivers = self.error_addresses
            subject, body, full_logs = self._render_mail(rebuild, success,
                                                         auto_canceled, manual_canceled)
            self._send_mail(receivers, subject, body, full_logs)
        else:
            self.log.info('conditions for sending notification not met, doing nothing')
