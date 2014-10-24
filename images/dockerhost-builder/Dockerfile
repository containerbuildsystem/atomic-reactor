FROM fedora:latest
RUN yum -y install docker-io git
ADD ./build.sh /tmp/build.sh
RUN chmod a+x /tmp/build.sh
CMD ["/tmp/build.sh"]
