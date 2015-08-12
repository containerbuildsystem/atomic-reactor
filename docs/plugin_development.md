Writing plugins for Atomic Reactor
==================================

For more information about plugins, see [plugins](https://github.com/projectatomic/atomic-reactor/blob/master/docs/plugins.md) document.

Let's create a plugin, which sends build log to provided URL.

## Clone Atomic Reactor

We'll use Atomic Reactor from git (you can also get it from your distribution, once it's there):

```
git clone https://github.com/projectatomic/atomic-reactor.git
cd atomic-reactor
```

Python should know where it can find our local copy of Atomic Reactor:

```
export PYTHONPATH="$(pwd):${PYTHONPATH}"
```

### Plugin

Time to create the plugin itself. Let's setup the directory first:

```
mkdir atomic-reactor-plugin-logs-submitter
cd atomic-reactor-plugin-logs-submitter/
```

Plugin code:

```python
from atomic_reactor.plugin import PostBuildPlugin

import requests

class LogSubmitter(PostBuildPlugin):

    # unique plugin identification
    # output of this plugin can be found in results specified with this key,
    # same thing goes for input: use this key to run the plugin
    key = "logs_submitter"

    # tasker and workflow are required arguments
    def __init__(self, tasker, workflow, url):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param url: str, URL where the logs should be posted
        """
        # call parent constructor: initialize tasker and workflow
        super(LogSubmitter, self).__init__(tasker, workflow)
        self.url = url

    def run(self):
        """
        each plugin has to implement this method — it is used to run the plugin actually

        response from this method is stored in `workflow.postbuild_results[self.key]`
        """
        json_data = {"logs": self.workflow.build_logs}
        return requests.post(self.url, json=json_data).content
```

There are two objects which enable you access to all the Atomic Reactor's magic:

1. **self.tasker** — instance of `atomic_reactor.core.DockerTasker`: it is a thin wrapper on top of [docker-py](https://github.com/docker/docker-py) — this is your access to docker
2. **self.workflow** — instance of `atomic_reactor.inner.DockerBuildWorkflow`: also contains a link, `self.workflow.builder`, to instance of `atomic_reactor.build.InsideBuilder` — these instances contain whole configuration, go ahead and change it however you want

Neat! Let's try our plugin. We'll have a webserver in terminal 1:

```
terminal1 $ ncat -kl localhost 9099
```

We also need an input json for the build itself (let's use my [hello world dockerfile](http://github.com/TomasTomecek/docker-hello-world)), the name of the file will be `build.json`:

```
{
        "image": "test-image",
        "git_url": "http://github.com/TomasTomecek/docker-hello-world",
        "postbuild_plugins": [{
                "name": "logs_submitter",
                "args": {
                    "url": "http://localhost:9099"
                }
        }]
}
```

Time to run the build (we'll build an image, `test-image`, in current environment (not inside a build container), getting data from `./build.json` and finally, we'll tell Atomic Reactor to load our plugin):

```
terminal2 $ atomic-reactor -v build json --method here ./build.json --load-plugin ./post_logs_submitter.py
...
2015-02-19 13:26:05,450 - atomic_reactor.plugin - DEBUG - running plugin 'logs_submitter' with args: '{u'url': u'http://localhost:9099'}'
```

What's in terminal 1?

```
terminal1 $ ncat -kl localhost 9099

POST / HTTP/1.1
Host: localhost:9099
Content-Length: 547
Accept-Encoding: gzip, deflate
Accept: */*
User-Agent: python-requests/2.5.0 CPython/2.7.8 Linux/3.18.6-200.fc21.x86_64
Connection: keep-alive
Content-Type: application/json

{"logs": ["{\"stream\":\"Step 0 : FROM fedora:latest\\n\"}\r\n", "{\"stream\":\" ---\\u003e 834629358fe2\\n\"}\r\n", "{\"stream\":\"Step 1 : RUN uname -a\\n\"}\r\n", "{\"stream\":\" ---\\u003e Running in b9207945f6fd\\n\"}\r\n", "{\"stream\":\"Linux c4e263145f81 3.18.6-200.fc21.x86_64 #1 SMP Fri Feb 6 22:59:42 UTC 2015 x86_64 x86_64 x86_64 GNU/Linux\\n\"}\r\n", "{\"stream\":\" ---\\u003e 48c3bcd190b1\\n\"}\r\n", "{\"stream\":\"Removing intermediate container b9207945f6fd\\n\"}\r\n", "{\"stream\":\"Successfully built 48c3bcd190b1\\n\"}\r\n"]}
```

As you can see, Atomic Reactor posted the json to the provided URL. Sweet!

'...now's ncat waiting with response — it may actually look stuck. Just hit "enter" or `ctrl+c`.'


And that's it.

