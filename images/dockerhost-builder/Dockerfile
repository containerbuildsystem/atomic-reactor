FROM fedora:latest
# e2fsprogs -- docker @ F20 wants it
RUN yum -y install docker git python-docker-py python-setuptools e2fsprogs koji python-backports-lzma osbs gssproxy
ADD ./atomic-reactor.tar.gz /tmp/
RUN cd /tmp/atomic-reactor-*/ && python setup.py install
CMD ["atomic-reactor", "--verbose", "inside-build"]
