FROM fedora:latest
# e2fsprogs -- docker @ F20 wants it
RUN yum -y install docker-io git python-docker-py python-setuptools GitPython e2fsprogs koji
ADD ./dock.tar.gz /tmp/
RUN cd /tmp/dock-*/ && python setup.py install
ADD ./docker.sh /tmp/docker.sh
RUN chmod a+x /tmp/docker.sh
CMD ["/tmp/docker.sh"]
