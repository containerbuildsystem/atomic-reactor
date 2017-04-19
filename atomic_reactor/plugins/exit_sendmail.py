"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from email.mime.text import MIMEText
import smtplib
import socket
try:
    from urlparse import urljoin
except ImportError:
    from urllib.parse import urljoin

from atomic_reactor.plugin import ExitPlugin, PluginFailedException
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.plugins.exit_koji_promote import KojiPromotePlugin
from atomic_reactor.koji_util import create_koji_session
from atomic_reactor.util import get_build_json


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
    key = "sendmail"

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
                 smtp_host, from_address,
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
                 to_koji_pkgowner=False):
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
        self.send_on = set(send_on)
        self.url = url
        self.additional_addresses = list(additional_addresses)
        self.smtp_host = smtp_host
        self.from_address = from_address
        self.error_addresses = list(error_addresses)
        self.email_domain = email_domain
        self.koji_hub = koji_hub
        self.koji_root = koji_root
        self.koji_auth_info = {
            'proxyuser': koji_proxyuser,
            'ssl_certs_dir': koji_ssl_certs_dir,
            'krb_principal': koji_krb_principal,
            'krb_keytab': koji_krb_keytab,
        }
        self.to_koji_submitter = to_koji_submitter
        self.to_koji_pkgowner = to_koji_pkgowner
        self.submitter = self.DEFAULT_SUBMITTER

        try:
            metadata = get_build_json().get("metadata", {})
            self.koji_task_id = int(metadata['labels']['koji-task-id'])
        except Exception:
            self.log.exception("Failed to fetch koji task ID")
            self.koji_task_id = None
        else:
            self.log.info("Koji task ID: %s", self.koji_task_id)

        try:
            self.koji_build_id = self.workflow.exit_results.get(KojiPromotePlugin.key)
        except Exception:
            self.log.exception("Failed to fetch koji build ID")
            self.koji_build_id = None
        else:
            self.log.info("Koji build ID: %s", self.koji_build_id)

        try:
            self.session = create_koji_session(self.koji_hub, self.koji_auth_info)
        except Exception:
            self.log.exception("Failed to connect to koji")
            self.session = None
        else:
            self.log.info("Koji connection established")

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

    def _render_mail(self, rebuild, success, auto_canceled, manual_canceled):
        """Render and return subject and body of the mail to send."""
        subject_template = '%(endstate)s building image %(image)s'
        body_template = '\n'.join([
            'Image: %(image)s',
            'Status: %(endstate)s',
            'Submitted by: %(user)s',
            'Logs: %(logs)s',
        ])

        endstate = None
        if auto_canceled or manual_canceled:
            endstate = 'Canceled'
        else:
            endstate = 'Succeeded' if success else 'Failed'

        url = self._get_logs_url()

        formatting_dict = {
            'image': self.workflow.image,
            'endstate': endstate,
            'user': '<autorebuild>' if rebuild else self.submitter,
            'logs': url
        }
        return (subject_template % formatting_dict, body_template % formatting_dict)

    def _send_mail(self, receivers_list, subject, body):
        """Actually sends the mail with `subject` and `body` to all members of `receivers_list`."""
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = self.from_address
        msg['To'] = ', '.join([x.strip() for x in receivers_list])

        s = None
        try:
            s = smtplib.SMTP(self.smtp_host)
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
        if not self.koji_task_id:
            return ""

        koji_task_info = self.session.getTaskInfo(self.koji_task_id)
        koji_task_owner = self.session.getUser(koji_task_info['owner'])
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
            # We're importing this here in order to trap ImportError
            from koji import PathInfo
            pathinfo = PathInfo(topdir=self.koji_root)
            url = urljoin(pathinfo.work(), pathinfo.taskrelpath(self.koji_task_id))
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

        # Remove empty and None items
        receivers_list = [x for x in receivers_list if x]

        if not receivers_list:
            raise RuntimeError("No recepients found")

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
                _, expected_body = self._render_mail(
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
            self.log.info('sending notification to %s ...', receivers)
            subject, body = self._render_mail(rebuild, success, auto_canceled, manual_canceled)
            self._send_mail(receivers, subject, body)
        else:
            self.log.info('conditions for sending notification not met, doing nothing')
